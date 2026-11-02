package session

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"pool/agent/enroll"
	"pool/agent/idle"
	"pool/agent/protocol"
	"pool/agent/sandbox"
	"pool/agent/sysinfo"
)

// ---------------------------------------------------------------------------
// Fake runtime
// ---------------------------------------------------------------------------

type fakeRuntime struct {
	dir     string
	mu      sync.Mutex
	block   bool
	release map[string]chan struct{}
	started map[string]chan struct{}
	cleaned map[string]bool
	stopped map[string]bool
	exit    int
}

func newFakeRuntime(t *testing.T) *fakeRuntime {
	return &fakeRuntime{dir: t.TempDir(), release: map[string]chan struct{}{}, started: map[string]chan struct{}{},
		cleaned: map[string]bool{}, stopped: map[string]bool{}}
}

func (f *fakeRuntime) Name() string { return "fake" }
func (f *fakeRuntime) Detect(context.Context) (sandbox.Info, error) {
	return sandbox.Info{Tiers: []protocol.Tier{protocol.T1, protocol.T2}, Rootless: true}, nil
}

func (f *fakeRuntime) ch(m map[string]chan struct{}, id string) chan struct{} {
	f.mu.Lock()
	defer f.mu.Unlock()
	if m[id] == nil {
		m[id] = make(chan struct{})
	}
	return m[id]
}

func (f *fakeRuntime) Run(ctx context.Context, s sandbox.Spec, onStart func(), logs sandbox.LogSink) (sandbox.Result, error) {
	onStart()
	close(f.ch(f.started, s.AttemptID))
	logs("stdout", []byte("hello from "+s.AttemptID+"\n"))
	f.mu.Lock()
	block := f.block
	f.mu.Unlock()
	if block {
		select {
		case <-ctx.Done():
			return sandbox.Result{}, sandbox.ErrKilled
		case <-f.ch(f.release, s.AttemptID):
		}
	}
	if ctx.Err() != nil {
		return sandbox.Result{}, sandbox.ErrKilled
	}
	body := []byte("seed=" + s.Env["SEED"] + "\n")
	path := filepath.Join(f.dir, s.AttemptID+"-result.txt")
	_ = os.WriteFile(path, body, 0o600)
	sum := sha256.Sum256(body)
	return sandbox.Result{ExitCode: f.exit, Outputs: []sandbox.Output{{
		ArtifactRef: protocol.ArtifactRef{Path: "result.txt", SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body))},
		LocalPath:   path,
	}}}, nil
}

func (f *fakeRuntime) Stop(_ context.Context, id string, _ time.Duration) error {
	f.mu.Lock()
	f.stopped[id] = true
	f.mu.Unlock()
	close(f.ch(f.release, id))
	return nil
}
func (f *fakeRuntime) Kill(context.Context, string) error              { return nil }
func (f *fakeRuntime) Pause(context.Context, string) error             { return nil }
func (f *fakeRuntime) Unpause(context.Context, string) error           { return nil }
func (f *fakeRuntime) Throttle(context.Context, string, float64) error { return nil }
func (f *fakeRuntime) KillAll(context.Context, string) (int, error)    { return 0, nil }
func (f *fakeRuntime) Cleanup(id string) error {
	f.mu.Lock()
	f.cleaned[id] = true
	f.mu.Unlock()
	return nil
}
func (f *fakeRuntime) wasCleaned(id string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cleaned[id]
}

// ---------------------------------------------------------------------------
// Fake control plane
// ---------------------------------------------------------------------------

type fakeServer struct {
	t       *testing.T
	srv     *httptest.Server
	signKey ed25519.PrivateKey
	devPub  ed25519.PublicKey
	conns   chan *serverConn
	uploads map[string][]byte
	mu      sync.Mutex
}

type serverConn struct {
	t     *testing.T
	c     *websocket.Conn
	in    chan map[string]any
	hello map[string]any
	wmu   sync.Mutex
}

func newFakeServer(t *testing.T, devPub ed25519.PublicKey) *fakeServer {
	_, signKey, _ := ed25519.GenerateKey(rand.Reader)
	fs := &fakeServer{t: t, signKey: signKey, devPub: devPub, conns: make(chan *serverConn, 4), uploads: map[string][]byte{}}
	up := websocket.Upgrader{}
	mux := http.NewServeMux()
	mux.HandleFunc(protocol.DefaultWSPath, func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		nonce := make([]byte, 32)
		_, _ = rand.Read(nonce)
		sc := &serverConn{t: t, c: c, in: make(chan map[string]any, 256)}
		sc.send(protocol.TypeChallenge, &protocol.Challenge{NonceB64: base64.StdEncoding.EncodeToString(nonce), ServerTime: time.Now()})
		_, raw, err := c.ReadMessage()
		if err != nil {
			return
		}
		var hello protocol.Hello
		if err := json.Unmarshal(raw, &hello); err != nil || hello.Type != "hello" {
			t.Errorf("bad hello: %s", raw)
			return
		}
		sig, _ := base64.StdEncoding.DecodeString(hello.NonceSignatureB64)
		if !ed25519.Verify(fs.devPub, append([]byte(protocol.HelloContext), nonce...), sig) {
			t.Errorf("hello signature invalid")
			return
		}
		_ = json.Unmarshal(raw, &sc.hello)
		for _, k := range []string{"running", "shares"} {
			if sc.hello[k] == nil {
				t.Errorf("hello.%s is null", k)
			}
		}
		caps := sc.hello["capabilities"].(map[string]any)
		for _, k := range []string{"gpus", "tiers", "gpu_tiers"} {
			if caps[k] == nil {
				t.Errorf("hello.capabilities.%s is null", k)
			}
		}
		sc.send(protocol.TypeWelcome, &protocol.Welcome{DeviceID: hello.DeviceID, HeartbeatIntervalS: 1, LeaseTTLS: 60,
			ServerTime: time.Now(), Stale: []string{}})
		go func() {
			defer close(sc.in)
			for {
				_, raw, err := c.ReadMessage()
				if err != nil {
					return
				}
				var m map[string]any
				_ = json.Unmarshal(raw, &m)
				if m["type"] == "heartbeat" {
					sc.send(protocol.TypeHeartbeatAck, &protocol.HeartbeatAck{LeaseExpiresAt: time.Now().Add(time.Minute)})
				}
				sc.in <- m
			}
		}()
		fs.conns <- sc
	})
	mux.HandleFunc("/upload/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			http.Error(w, "method", 405)
			return
		}
		b, _ := io.ReadAll(r.Body)
		fs.mu.Lock()
		fs.uploads[strings.TrimPrefix(r.URL.Path, "/upload/")] = b
		fs.mu.Unlock()
	})
	fs.srv = httptest.NewServer(mux)
	t.Cleanup(fs.srv.Close)
	return fs
}

func (s *serverConn) send(typ string, msg any) {
	s.wmu.Lock()
	defer s.wmu.Unlock()
	raw, err := protocol.Encode(typ, msg)
	if err != nil {
		s.t.Fatal(err)
	}
	_ = s.c.WriteMessage(websocket.TextMessage, raw)
}

// expect waits for a message of the given type, skipping heartbeats and
// readies unless asked for.
func (s *serverConn) expect(typ string, timeout time.Duration) map[string]any {
	s.t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case m, ok := <-s.in:
			if !ok {
				s.t.Fatalf("connection closed while waiting for %s", typ)
			}
			if m["type"] == typ {
				return m
			}
		case <-deadline:
			s.t.Fatalf("timed out waiting for %s", typ)
		}
	}
}

// none asserts that no message of the given types arrives within d.
func (s *serverConn) none(d time.Duration, types ...string) {
	s.t.Helper()
	deadline := time.After(d)
	for {
		select {
		case m, ok := <-s.in:
			if !ok {
				return
			}
			for _, typ := range types {
				if m["type"] == typ {
					s.t.Fatalf("unexpected %s: %v", typ, m)
				}
			}
		case <-deadline:
			return
		}
	}
}

func (fs *fakeServer) offer(deviceID string, mutate func(*protocol.TaskEnvelope), key ed25519.PrivateKey) *protocol.Offer {
	env := protocol.TaskEnvelope{
		V: protocol.Version, AttemptID: protocol.NewID(), TaskID: protocol.NewID(), JobID: protocol.NewID(), Epoch: 1,
		Group: "research-team", GroupID: protocol.NewID(), Submitter: "alice", DeviceID: deviceID,
		IssuedAt: time.Now(), ExpiresAt: time.Now().Add(time.Minute),
		Spec: protocol.TaskSpec{
			Image: "busybox@sha256:" + strings.Repeat("a", 64), Command: []string{"true"}, Env: map[string]string{"SEED": "7"},
			Resources: protocol.TaskResources{CPU: 1, MemoryMB: 256}, MinTier: protocol.T1,
			Network: protocol.TaskNetwork{Egress: "none", Allow: []string{}}, DataClass: "internal", MaxRuntimeS: 60,
			OutputsPath: "/out", Inputs: []protocol.TaskInput{},
		},
	}
	if mutate != nil {
		mutate(&env)
	}
	payload, _ := json.Marshal(env)
	if key == nil {
		key = fs.signKey
	}
	return &protocol.Offer{
		AttemptID: env.AttemptID, TaskID: env.TaskID, Epoch: env.Epoch, ExpiresAt: env.ExpiresAt,
		Envelope: protocol.SignedEnvelope{KeyID: "k1", PayloadB64: base64.StdEncoding.EncodeToString(payload),
			SignatureB64: base64.StdEncoding.EncodeToString(ed25519.Sign(key, payload))},
	}
}

const testPolicy = `
shares:
  - group: research-team
    limits: { cpu_percent: 100, memory: 8Gi, disk: 10Gi }
    accept_from: all-members
idle: assume-idle
`

type harness struct {
	t      *testing.T
	fs     *fakeServer
	rt     *fakeRuntime
	agent  *Agent
	prof   *enroll.Profile
	home   string
	cancel context.CancelFunc
	done   chan struct{}
}

func start(t *testing.T, block bool) *harness {
	t.Helper()
	rt := newFakeRuntime(t)
	rt.block = block
	h := startWith(t, rt)
	h.rt = rt
	return h
}

func startWith(t *testing.T, rt sandbox.Runtime) *harness {
	t.Helper()
	home := t.TempDir()
	dir := filepath.Join(home, "profiles", "default")
	_ = os.MkdirAll(dir, 0o700)
	devPub, devKey, _ := ed25519.GenerateKey(rand.Reader)
	fs := newFakeServer(t, devPub)
	prof := &enroll.Profile{
		Name: "default", Server: fs.srv.URL, DeviceID: protocol.NewID(), WSPath: protocol.DefaultWSPath,
		SigningKeys: []protocol.SigningKey{{KeyID: "k1",
			PublicKeyB64: base64.StdEncoding.EncodeToString(fs.signKey.Public().(ed25519.PublicKey))}},
		CertNotAfter: time.Now().Add(24 * time.Hour), InsecureHTTP: true, Dir: dir, Key: devKey,
		CertPEM: []byte("-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"),
	}
	_ = os.WriteFile(filepath.Join(dir, enroll.PolicyFile), []byte(testPolicy), 0o600)
	a, err := New(Config{
		Profile: prof, Home: home, Runtime: rt, AgentVersion: "test", Idle: idle.Static{Idle: true},
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		DeviceInfo: func() sysinfo.Info {
			return sysinfo.Info{OS: "linux", Arch: "amd64", CPUCores: 8, MemoryMB: 16384, FreeDiskMB: 100000, GPUs: []protocol.GPUInfo{}}
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	h := &harness{t: t, fs: fs, agent: a, prof: prof, home: home, cancel: cancel, done: make(chan struct{})}
	go func() { _ = a.Run(ctx); close(h.done) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-h.done:
		case <-time.After(10 * time.Second):
			t.Error("agent did not stop")
		}
	})
	return h
}

func (h *harness) conn() *serverConn {
	h.t.Helper()
	select {
	case c := <-h.fs.conns:
		return c
	case <-time.After(15 * time.Second):
		h.t.Fatal("agent did not connect")
	}
	return nil
}

func TestHappyPath(t *testing.T) {
	h := start(t, false)
	c := h.conn()
	if c.hello["profile"] != "default" || c.hello["policy_digest"] == "" {
		t.Fatalf("hello: %v", c.hello)
	}
	ready := c.expect("ready", 5*time.Second)
	free := ready["free"].(map[string]any)
	if free["cpu"].(float64) != 8 || len(ready["tiers"].([]any)) != 2 {
		t.Fatalf("ready: %v", ready)
	}
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	acc := c.expect("accept", 5*time.Second)
	if acc["attempt_id"] != o.AttemptID || acc["tier"] != "T2" || acc["epoch"].(float64) != 1 {
		t.Fatalf("accept: %v", acc)
	}
	c.expect("started", 5*time.Second)
	prog := c.expect("progress", 5*time.Second)
	if !strings.Contains(prog["log_chunk"].(string), "hello from") || prog["seq"].(float64) != 0 {
		t.Fatalf("progress: %v", prog)
	}
	ur := c.expect("upload_request", 5*time.Second)
	arts := ur["artifacts"].([]any)
	art := arts[0].(map[string]any)
	c.send(protocol.TypeUploadURLs, &protocol.UploadURLs{AttemptID: o.AttemptID, URLs: []protocol.UploadURL{{
		Path: art["path"].(string), SHA256: art["sha256"].(string), URL: h.fs.srv.URL + "/upload/" + art["sha256"].(string),
		ExpiresAt: time.Now().Add(15 * time.Minute)}}})
	commit := c.expect("commit", 5*time.Second)
	if commit["exit_code"].(float64) != 0 || commit["idempotency_key"] != o.TaskID+":"+o.AttemptID {
		t.Fatalf("commit: %v", commit)
	}
	outs := commit["outputs"].([]any)
	if len(outs) != 1 || outs[0].(map[string]any)["sha256"] != art["sha256"] {
		t.Fatalf("outputs: %v", outs)
	}
	h.fs.mu.Lock()
	body := h.fs.uploads[art["sha256"].(string)]
	h.fs.mu.Unlock()
	if string(body) != "seed=7\n" {
		t.Fatalf("uploaded body %q", body)
	}
	if h.rt.wasCleaned(o.AttemptID) {
		t.Fatal("cleaned up before commit_ack")
	}
	c.send(protocol.TypeCommitAck, &protocol.CommitAck{AttemptID: o.AttemptID, Accepted: true})
	waitFor(t, func() bool { return h.rt.wasCleaned(o.AttemptID) }, "cleanup after commit_ack")
	st, err := ReadState(h.prof.Dir)
	if err != nil || !st.Connected || len(st.Attempts) != 0 {
		t.Fatalf("state: %+v %v", st, err)
	}
	// The agent keeps one ready outstanding; after no_work it asks again.
	c.send(protocol.TypeNoWork, &protocol.NoWork{RetryAfterS: 1})
	ready = c.expect("ready", 10*time.Second)
	if ready["free"].(map[string]any)["cpu"].(float64) != 8 {
		t.Fatalf("capacity not released: %v", ready)
	}
}

func TestDeclines(t *testing.T) {
	h := start(t, false)
	c := h.conn()
	_, otherKey, _ := ed25519.GenerateKey(rand.Reader)
	cases := []struct {
		name   string
		offer  *protocol.Offer
		reason string
	}{
		{"bad signature", h.fs.offer(h.prof.DeviceID, nil, otherKey), protocol.DeclineSignatureInvalid},
		{"wrong device", h.fs.offer(protocol.NewID(), nil, nil), protocol.DeclineWrongDevice},
		{"expired", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.ExpiresAt = time.Now().Add(-time.Second) }, nil), protocol.DeclineEnvelopeExpired},
		{"group not shared", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.Group = "other-group" }, nil), protocol.DeclineGroupNotShared},
		{"egress", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.Spec.Network.Egress = "allowlist" }, nil), protocol.DeclineEgressNotAllowed},
		{"unpinned image", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.Spec.Image = "busybox:latest" }, nil), protocol.DeclineImageNotAllowed},
		{"too big", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.Spec.Resources.CPU = 64 }, nil), protocol.DeclineResourcesExceedLimits},
		{"min tier T3", h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) { e.Spec.MinTier = protocol.T3 }, nil), protocol.DeclineTierNotAllowed},
	}
	for _, tc := range cases {
		c.send(protocol.TypeOffer, tc.offer)
		d := c.expect("decline", 5*time.Second)
		if d["reason"] != tc.reason || d["attempt_id"] != tc.offer.AttemptID {
			t.Errorf("%s: got %v, want %s", tc.name, d, tc.reason)
		}
	}
	// A tampered envelope (payload changed after signing) is refused too.
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	payload, _ := base64.StdEncoding.DecodeString(o.Envelope.PayloadB64)
	o.Envelope.PayloadB64 = base64.StdEncoding.EncodeToString([]byte(strings.Replace(string(payload), `"submitter":"alice"`, `"submitter":"bob"`, 1)))
	c.send(protocol.TypeOffer, o)
	if d := c.expect("decline", 5*time.Second); d["reason"] != protocol.DeclineSignatureInvalid {
		t.Fatalf("tampered: %v", d)
	}
}

func TestStaleLeaseStopsWithoutCommit(t *testing.T) {
	h := start(t, true)
	c := h.conn()
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	c.expect("accept", 5*time.Second)
	c.expect("started", 5*time.Second)
	hb := c.expect("heartbeat", 5*time.Second)
	if running := hb["running"].([]any); len(running) != 1 || running[0].(map[string]any)["attempt_id"] != o.AttemptID {
		t.Fatalf("heartbeat running: %v", hb["running"])
	}
	c.send(protocol.TypeHeartbeatAck, &protocol.HeartbeatAck{Stale: []string{o.AttemptID}, LeaseExpiresAt: time.Now()})
	waitFor(t, func() bool { return h.rt.wasCleaned(o.AttemptID) }, "stale attempt cleaned up")
	c.none(2*time.Second, "commit", "abort", "upload_request")
}

func TestKillSwitch(t *testing.T) {
	h := start(t, true)
	c := h.conn()
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	c.expect("accept", 5*time.Second)
	c.expect("started", 5*time.Second)
	if err := os.WriteFile(filepath.Join(h.home, enroll.KillFile), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ab := c.expect("abort", 5*time.Second)
	if ab["reason"] != protocol.AbortKillSwitch || ab["attempt_id"] != o.AttemptID {
		t.Fatalf("abort: %v", ab)
	}
	c.none(3*time.Second, "ready", "commit")
	// Offers are declined while the switch is on.
	o2 := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o2)
	if d := c.expect("decline", 5*time.Second); d["reason"] != protocol.DeclineKillSwitch {
		t.Fatalf("decline: %v", d)
	}
	_ = os.Remove(filepath.Join(h.home, enroll.KillFile))
	c.expect("ready", 10*time.Second)
}

func TestReconnectBuffersCommit(t *testing.T) {
	h := start(t, true)
	c := h.conn()
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	c.expect("accept", 5*time.Second)
	c.expect("started", 5*time.Second)
	_ = c.c.Close() // network drop
	close(h.rt.ch(h.rt.release, o.AttemptID))
	c2 := h.conn()
	running := c2.hello["running"].([]any)
	if len(running) != 1 || running[0].(map[string]any)["attempt_id"] != o.AttemptID {
		t.Fatalf("hello after reconnect should report the attempt: %v", c2.hello["running"])
	}
	commit := c2.expect("commit", 10*time.Second)
	if commit["attempt_id"] != o.AttemptID || commit["epoch"].(float64) != 1 {
		t.Fatalf("commit: %v", commit)
	}
	c2.send(protocol.TypeCommitAck, &protocol.CommitAck{AttemptID: o.AttemptID, Accepted: false})
	waitFor(t, func() bool { return h.rt.wasCleaned(o.AttemptID) }, "cleanup after rejected commit")
}

func TestServerCancel(t *testing.T) {
	h := start(t, true)
	c := h.conn()
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	c.expect("accept", 5*time.Second)
	c.expect("started", 5*time.Second)
	c.send(protocol.TypeRevoke, &protocol.Revoke{Group: "research-team", Reason: "member removed"})
	waitFor(t, func() bool { return h.rt.wasCleaned(o.AttemptID) }, "revoked attempt cleaned up")
	c.none(2*time.Second, "commit", "abort")
}

func waitFor(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out: %s", what)
}

type toggleIdle struct {
	mu   sync.Mutex
	idle bool
}

func (d *toggleIdle) set(v bool)   { d.mu.Lock(); d.idle = v; d.mu.Unlock() }
func (d *toggleIdle) Name() string { return "toggle" }
func (d *toggleIdle) IdleFor() (time.Duration, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.idle {
		return time.Hour, nil
	}
	return 0, nil
}

func TestOwnerReturnCheckpointAndStop(t *testing.T) {
	h := start(t, true)
	det := &toggleIdle{idle: true}
	// Swap in a policy that needs an idle owner, and a detector we control.
	h.agent.mu.Lock()
	h.agent.idleDet = det
	h.agent.cfg.Idle = det
	h.agent.mu.Unlock()
	_ = os.WriteFile(h.agent.cfg.PolicyPath, []byte(strings.Replace(testPolicy, "accept_from: all-members",
		"accept_from: all-members\n    when: { only_when_idle: 10m }\n    on_owner_return: checkpoint-and-stop", 1)), 0o600)
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(h.agent.cfg.PolicyPath, future, future)
	c := h.conn()
	waitFor(t, func() bool {
		h.agent.mu.Lock()
		defer h.agent.mu.Unlock()
		return h.agent.pol.Find("research-team").When.IdleRequired() == 10*time.Minute
	}, "policy reload")
	o := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o)
	c.expect("accept", 5*time.Second)
	c.expect("started", 5*time.Second)
	det.set(false) // owner comes back
	ab := c.expect("abort", 15*time.Second)
	if ab["reason"] != protocol.AbortOwnerReturned {
		t.Fatalf("abort: %v", ab)
	}
	h.rt.mu.Lock()
	stopped := h.rt.stopped[o.AttemptID]
	h.rt.mu.Unlock()
	if !stopped {
		t.Fatal("container was not stopped gracefully (SIGTERM for checkpoint)")
	}
	// While the owner is active, offers for the share are declined.
	o2 := h.fs.offer(h.prof.DeviceID, nil, nil)
	c.send(protocol.TypeOffer, o2)
	if d := c.expect("decline", 10*time.Second); d["reason"] != protocol.DeclineOwnerActive {
		t.Fatalf("decline: %v", d)
	}
}
