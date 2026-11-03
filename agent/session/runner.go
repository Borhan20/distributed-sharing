package session

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"pool/agent/enroll"
	"pool/agent/policy"
	"pool/agent/protocol"
	"pool/agent/registry"
	"pool/agent/sandbox"
)

// runAttempt runs one accepted attempt to the end and queues its result.
func (a *Agent) runAttempt(ctx context.Context, at *attempt) {
	env := at.env
	spec := sandbox.Spec{
		AttemptID: at.id, TaskID: at.taskID, Epoch: at.epoch, Group: at.group, Profile: a.cfg.Profile.Name,
		Tier: at.tier, Image: env.Spec.Image, Command: env.Spec.Command, Env: env.Spec.Env,
		TaskIndex: env.Spec.TaskIndex, TaskCount: env.Spec.TaskCount,
		CPU: env.Spec.Resources.CPU, MemoryMB: env.Spec.Resources.MemoryMB, DiskMB: env.Spec.Resources.DiskMB,
		GPUs: env.Spec.Resources.GPUs, MaxRuntime: time.Duration(env.Spec.MaxRuntimeS) * time.Second,
		OutputsPath: env.Spec.OutputsPath,
	}
	onStart := func() {
		a.mu.Lock()
		at.state, at.startedAt = "running", a.now()
		a.mu.Unlock()
		a.enqueue(protocol.TypeStarted, &protocol.Started{AttemptID: at.id, Epoch: at.epoch}, 0)
		a.writeState()
	}
	logs := func(stream string, b []byte) { a.enqueueLog(at, stream, b) }
	var res sandbox.Result
	var err error
	if env.RegistryAuth {
		spec.RegistryAuth, err = a.pullAuth(at)
		if err == nil {
			a.markPrivate(env.Spec.Image)
		}
	} else if a.isPrivate(env.Spec.Image) {
		// This computer pulled the image for a group with a token. This group has
		// none: it may use the local copy only if anyone could pull the image.
		check := a.cfg.CheckPublic
		if check == nil {
			check = func(ctx context.Context, image string) error { return registry.CheckPublic(ctx, nil, image) }
		}
		if cerr := check(ctx, env.Spec.Image); cerr != nil {
			err = fmt.Errorf("this image is private: this computer pulled it for another group, and this group "+
				"has no access to it (%v)", cerr)
		} else {
			a.markPublic(env.Spec.Image)
		}
	}
	if err == nil {
		res, err = a.cfg.Runtime.Run(ctx, spec, onStart, logs)
	}

	a.mu.Lock()
	dropped, reason := at.dropped, at.stopReason
	at.state = "finishing"
	a.mu.Unlock()
	switch {
	case dropped:
		a.finishAttempt(at.id)
		return
	case reason != "":
		detail := map[string]string{
			protocol.AbortKillSwitch:    "the device owner used the kill switch",
			protocol.AbortOwnerReturned: "the device owner returned",
			protocol.AbortPolicy:        "the local policy no longer shares this device with the group",
			protocol.AbortAgentShutdown: "the agent is shutting down",
		}[reason]
		a.enqueue(protocol.TypeAbort, &protocol.Abort{AttemptID: at.id, Epoch: at.epoch, Reason: reason, Detail: detail}, 0)
		a.finishAttempt(at.id)
		return
	case err != nil:
		reason := protocol.AbortRuntimeError
		if errors.Is(err, sandbox.ErrKilled) {
			// Killed by something outside the agent (e.g. `pool-agent kill`).
			reason = protocol.AbortKillSwitch
		}
		a.log.Warn("attempt failed to run", "attempt", at.id, "err", err)
		a.agentLog(at, "agent: "+err.Error())
		a.enqueue(protocol.TypeAbort, &protocol.Abort{AttemptID: at.id, Epoch: at.epoch, Reason: reason, Detail: truncate(err.Error(), 1000)}, 0)
		a.finishAttempt(at.id)
		return
	}
	for _, n := range res.Notes {
		a.agentLog(at, "agent: "+n)
	}
	refs := a.upload(at, res.Outputs)
	commit := &protocol.Commit{
		IdempotencyKey: at.taskID + ":" + at.id, TaskID: at.taskID, AttemptID: at.id, Epoch: at.epoch,
		ExitCode: res.ExitCode, Outputs: refs,
	}
	if res.TimedOut {
		msg := fmt.Sprintf("max_runtime_exceeded (%ds)", env.Spec.MaxRuntimeS)
		commit.Error = &msg
	}
	a.mu.Lock()
	if at.dropped {
		a.mu.Unlock()
		a.finishAttempt(at.id)
		return
	}
	at.commit = commit
	a.mu.Unlock()
	a.log.Info("attempt finished", "attempt", at.id, "exit_code", res.ExitCode, "outputs", len(refs))
	a.writeState()
	a.poke()
}

func (a *Agent) enqueueLog(at *attempt, stream string, b []byte) {
	for len(b) > 0 {
		n := len(b)
		if n > logChunkBytes {
			n = logChunkBytes
		}
		chunk := strings.ToValidUTF8(string(b[:n]), "�")
		b = b[n:]
		a.mu.Lock()
		seq := at.seq
		at.seq++
		a.mu.Unlock()
		a.enqueue(protocol.TypeProgress, &protocol.Progress{
			AttemptID: at.id, Epoch: at.epoch, Seq: seq, Stream: stream, LogChunk: chunk,
		}, len(chunk))
	}
}

func (a *Agent) agentLog(at *attempt, text string) {
	a.enqueueLog(at, "agent", []byte(text+"\n"))
}

// pullAuth asks the server for a short-lived token to pull a private image.
// Without one the attempt fails: a private image is never pulled anonymously.
func (a *Agent) pullAuth(at *attempt) (*sandbox.RegistryAuth, error) {
	ch := make(chan protocol.PullAuth, 1)
	a.mu.Lock()
	connected := a.conn != nil
	if connected {
		a.pullWait[at.id] = ch
	}
	a.mu.Unlock()
	if !connected {
		return nil, errors.New("private image: not connected to the server to get a pull token")
	}
	if err := a.send(protocol.TypePullAuthReq, &protocol.PullAuthRequest{AttemptID: at.id, Epoch: at.epoch}); err != nil {
		a.mu.Lock()
		delete(a.pullWait, at.id)
		a.mu.Unlock()
		return nil, fmt.Errorf("private image: %w", err)
	}
	select {
	case m, ok := <-ch:
		if !ok {
			return nil, errors.New("private image: disconnected before the pull token arrived")
		}
		if m.Token == nil || m.Registry == nil || *m.Token == "" {
			reason := "no reason given"
			if m.Reason != nil {
				reason = *m.Reason
			}
			return nil, fmt.Errorf("private image: the server gave no pull token (%s)", reason)
		}
		return &sandbox.RegistryAuth{Key: *m.Registry, Token: *m.Token}, nil
	case <-time.After(pullAuthTimeout):
		a.mu.Lock()
		delete(a.pullWait, at.id)
		a.mu.Unlock()
		return nil, errors.New("private image: no pull token from the server")
	}
}

// upload asks for presigned URLs and PUTs every output. Outputs are listed
// in the commit even if they could not be uploaded.
func (a *Agent) upload(at *attempt, outs []sandbox.Output) []protocol.ArtifactRef {
	refs := make([]protocol.ArtifactRef, 0, len(outs))
	for _, o := range outs {
		refs = append(refs, o.ArtifactRef)
	}
	if len(outs) == 0 {
		return refs
	}
	ch := make(chan protocol.UploadURLs, 1)
	a.mu.Lock()
	connected := a.conn != nil
	if connected {
		a.uploadWait[at.id] = ch
	}
	a.mu.Unlock()
	if !connected {
		a.agentLog(at, "agent: not connected; outputs were not uploaded")
		return refs
	}
	// Deliver buffered logs first so the server sees them before the upload
	// request and the commit (which flush sends after the outbox).
	err := a.flush()
	if err == nil {
		err = a.send(protocol.TypeUploadRequest, &protocol.UploadRequest{AttemptID: at.id, Epoch: at.epoch, Artifacts: refs})
	}
	if err != nil {
		a.mu.Lock()
		delete(a.uploadWait, at.id)
		a.mu.Unlock()
		a.agentLog(at, "agent: upload request not sent: "+err.Error())
		return refs
	}
	var urls protocol.UploadURLs
	select {
	case u, ok := <-ch:
		if !ok {
			a.agentLog(at, "agent: disconnected before upload URLs arrived; outputs were not uploaded")
			return refs
		}
		urls = u
	case <-time.After(uploadURLTimeout):
		a.mu.Lock()
		delete(a.uploadWait, at.id)
		a.mu.Unlock()
		a.agentLog(at, "agent: no upload URLs from server; outputs were not uploaded")
		return refs
	}
	if len(urls.URLs) == 0 {
		reason := "none given"
		if urls.Reason != nil {
			reason = *urls.Reason
		}
		a.agentLog(at, "agent: outputs kept on the device only until cleanup (server: "+reason+")")
		return refs
	}
	byKey := map[string]sandbox.Output{}
	for _, o := range outs {
		byKey[o.Path+"\x00"+o.SHA256] = o
	}
	client := a.cfg.HTTPClient
	if client == nil {
		tlsCfg, err := a.cfg.Profile.UploadTLSConfig()
		if err != nil {
			a.agentLog(at, "agent: outputs were not uploaded: "+err.Error())
			return refs
		}
		client = &http.Client{Timeout: 30 * time.Minute,
			Transport: &http.Transport{TLSClientConfig: tlsCfg, Proxy: http.ProxyFromEnvironment}}
	}
	uploaded := 0
	for _, u := range urls.URLs {
		o, ok := byKey[u.Path+"\x00"+u.SHA256]
		if !ok {
			continue
		}
		if err := a.put(client, u.URL, o); err != nil {
			a.agentLog(at, fmt.Sprintf("agent: upload of %s failed: %v", o.Path, err))
			continue
		}
		uploaded++
	}
	a.agentLog(at, fmt.Sprintf("agent: uploaded %d of %d outputs", uploaded, len(outs)))
	return refs
}

// put uploads one file. Only https URLs are used, except plain http when
// the profile itself is a localhost dev profile: the URL comes from the
// server, and the agent must not be turned into a client for arbitrary
// LAN addresses.
func (a *Agent) put(client *http.Client, raw string, o sandbox.Output) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if u.Scheme != "https" && !(u.Scheme == "http" && a.cfg.Profile.InsecureHTTP) {
		return fmt.Errorf("refusing non-https upload URL")
	}
	f, err := os.Open(o.LocalPath)
	if err != nil {
		return err
	}
	defer f.Close()
	req, err := http.NewRequest(http.MethodPut, raw, f)
	if err != nil {
		return err
	}
	req.ContentLength = o.Size
	if o.Size == 0 {
		req.Body = http.NoBody
	}
	// When the server signed the object's SHA-256 into the URL, the store
	// requires the header and rejects a body that does not match it.
	if signsHeader(u, checksumHeader) {
		sum, err := hex.DecodeString(o.SHA256)
		if err != nil || len(sum) != sha256.Size {
			return fmt.Errorf("bad sha256 for %s", o.Path)
		}
		req.Header.Set(checksumHeader, base64.StdEncoding.EncodeToString(sum))
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("HTTP %s", resp.Status)
	}
	return nil
}

const checksumHeader = "x-amz-checksum-sha256"

// signsHeader reports whether a SigV4 presigned URL lists header among its
// signed headers (X-Amz-SignedHeaders=content-length;host;...).
func signsHeader(u *url.URL, header string) bool {
	for _, h := range strings.Split(u.Query().Get("X-Amz-SignedHeaders"), ";") {
		if strings.EqualFold(h, header) {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ---------------------------------------------------------------------------
// Local state file for `pool-agent status`
// ---------------------------------------------------------------------------

// State is written to <profile>/state.json whenever something changes.
type State struct {
	Profile    string         `json:"profile"`
	DeviceID   string         `json:"device_id"`
	Server     string         `json:"server"`
	Connected  bool           `json:"connected"`
	KillSwitch bool           `json:"kill_switch"`
	UpdatedAt  time.Time      `json:"updated_at"`
	Attempts   []AttemptState `json:"attempts"`
}

// AttemptState is one running attempt as shown to the owner ("what runs
// on my computer and who sent it", plan §8.1 principle 5).
type AttemptState struct {
	AttemptID   string        `json:"attempt_id"`
	TaskID      string        `json:"task_id"`
	Epoch       int64         `json:"epoch"`
	Group       string        `json:"group"`
	Submitter   string        `json:"submitter"`
	Image       string        `json:"image"`
	Tier        protocol.Tier `json:"tier"`
	State       string        `json:"state"`
	OwnerAction string        `json:"owner_action,omitempty"`
	StartedAt   *time.Time    `json:"started_at,omitempty"`
}

func (a *Agent) writeState() {
	a.mu.Lock()
	st := State{
		Profile: a.cfg.Profile.Name, DeviceID: a.cfg.Profile.DeviceID, Server: a.cfg.Profile.Server,
		Connected: a.conn != nil, KillSwitch: a.killed, UpdatedAt: a.now().UTC(), Attempts: []AttemptState{},
	}
	for _, at := range a.attempts {
		s := AttemptState{
			AttemptID: at.id, TaskID: at.taskID, Epoch: at.epoch, Group: at.group, Submitter: at.submitter,
			Image: at.image, Tier: at.tier, State: at.state, OwnerAction: at.ownerAction,
		}
		if !at.startedAt.IsZero() {
			t := at.startedAt.UTC()
			s.StartedAt = &t
		}
		st.Attempts = append(st.Attempts, s)
	}
	a.mu.Unlock()
	sort.Slice(st.Attempts, func(i, j int) bool { return st.Attempts[i].AttemptID < st.Attempts[j].AttemptID })
	raw, _ := json.MarshalIndent(st, "", "  ")
	path := filepath.Join(a.cfg.Profile.Dir, enroll.StateFile)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(raw, '\n'), 0o600); err == nil {
		_ = os.Rename(tmp, path)
	}
}

// ReadState reads a profile's state file.
func ReadState(profileDir string) (*State, error) {
	raw, err := os.ReadFile(filepath.Join(profileDir, enroll.StateFile))
	if err != nil {
		return nil, err
	}
	var st State
	return &st, json.Unmarshal(raw, &st)
}

// ---------------------------------------------------------------------------
// Device-wide capacity shared by all profiles
// ---------------------------------------------------------------------------

// Capacity tracks resources reserved across profiles so two servers cannot
// oversubscribe one device (plan §7.11).
type Capacity struct {
	mu   sync.Mutex
	used map[string]protocol.TaskResources
}

func NewCapacity() *Capacity { return &Capacity{used: map[string]protocol.TaskResources{}} }

// Reserve claims resources if the device has them free.
func (c *Capacity) Reserve(key string, r protocol.TaskResources, dev policy.Device) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	total := c.totalLocked()
	if total.CPU+r.CPU > float64(dev.CPUCores)+1e-9 {
		return false
	}
	if dev.MemoryMB > 0 && total.MemoryMB+r.MemoryMB > dev.MemoryMB {
		return false
	}
	if total.GPUs+r.GPUs > len(dev.GPUs) && r.GPUs > 0 {
		return false
	}
	c.used[key] = r
	return true
}

func (c *Capacity) Release(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.used, key)
}

func (c *Capacity) Total() policy.Used {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.totalLocked()
}

func (c *Capacity) totalLocked() policy.Used {
	var u policy.Used
	for _, r := range c.used {
		u = u.Add(r)
	}
	return u
}
