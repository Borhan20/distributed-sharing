// Package session runs one enrolled profile: it keeps the WebSocket to the
// control plane open, admits offers under the local policy, runs attempts in
// the sandbox and reports results (plan §7.1-7.6, design doc §3-4).
package session

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"pool/agent/enroll"
	"pool/agent/idle"
	"pool/agent/policy"
	"pool/agent/protocol"
	"pool/agent/sandbox"
	"pool/agent/sysinfo"
)

const (
	maxFrameBytes      = 4 << 20
	maxOutboxProgress  = 8 << 20 // bytes of buffered logs kept while disconnected
	logChunkBytes      = 16 * 1024
	readyTimeout       = 30 * time.Second
	uploadURLTimeout   = 60 * time.Second
	pullAuthTimeout    = 30 * time.Second
	ownerCheckInterval = 5 * time.Second
	stopGrace          = 30 * time.Second
)

// Config for one profile.
type Config struct {
	Profile      *enroll.Profile
	Home         string
	PolicyPath   string
	Runtime      sandbox.Runtime
	Capacity     *Capacity
	AgentVersion string
	Logger       *slog.Logger

	// Optional overrides (tests).
	Idle       idle.Detector
	Now        func() time.Time
	DeviceInfo func() sysinfo.Info
	HTTPClient *http.Client
	Dialer     *websocket.Dialer
	// CheckPublic reports whether anyone can pull an image (default: ask its
	// registry, registry.CheckPublic). Tests replace it.
	CheckPublic func(ctx context.Context, image string) error
}

// Agent runs one profile.
type Agent struct {
	cfg  Config
	log  *slog.Logger
	keys map[string]ed25519.PublicKey
	os   string
	arch string

	mu           sync.Mutex
	pol          *policy.Policy
	polMod       time.Time
	idleDet      idle.Detector
	idleState    policy.IdleState
	idleChecked  time.Time
	rtInfo       sandbox.Info
	dev          policy.Device
	attempts     map[string]*attempt
	outbox       []outMsg
	outboxBytes  int
	conn         *websocket.Conn
	connID       int64
	killed       bool
	readyPending bool
	readySentAt  time.Time
	nextReadyAt  time.Time
	renewPending bool
	renewSentAt  time.Time
	hbInterval   time.Duration
	leaseTTL     time.Duration
	uploadWait   map[string]chan protocol.UploadURLs
	pullWait     map[string]chan protocol.PullAuth
	tunnels      int // app connections open now (at most maxTunnels)
	// Images this computer pulled with a group's token ("token") or found to
	// be public ("public"); private-images.json in the profile directory, so it
	// survives restarts.
	privateImages map[string]string
	// Digests of images this computer ran, newest last (at most maxRecentImages): sent with Ready.
	recentImages []string

	writeMu sync.Mutex
	wake    chan struct{}
}

type outMsg struct {
	typ      string
	msg      any
	progress int // bytes, for log chunks that may be dropped
}

type attempt struct {
	id        string
	taskID    string
	epoch     int64
	group     string
	submitter string
	image     string
	tier      protocol.Tier
	res       protocol.TaskResources
	env       *protocol.TaskEnvelope
	state     string // assigned | running | finishing
	seq       int64
	startedAt time.Time
	cancel    context.CancelFunc

	ownerAction string // "", "paused", "throttled", "stopping"
	stopReason  string // why we stopped it ourselves: kill_switch, policy, owner_returned
	dropped     bool   // server no longer wants it (stale, cancel, preempt, revoke)

	commit       *protocol.Commit
	commitSentOn int64
}

// New prepares an agent for a profile.
func New(cfg Config) (*Agent, error) {
	if cfg.Profile == nil || cfg.Runtime == nil {
		return nil, errors.New("session: profile and runtime are required")
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.Capacity == nil {
		cfg.Capacity = NewCapacity()
	}
	if cfg.PolicyPath == "" {
		cfg.PolicyPath = filepath.Join(cfg.Profile.Dir, enroll.PolicyFile)
	}
	if cfg.DeviceInfo == nil {
		workDir := filepath.Join(cfg.Profile.Dir, enroll.WorkDir)
		cfg.DeviceInfo = func() sysinfo.Info { return sysinfo.Collect(workDir) }
	}
	keyMap, err := cfg.Profile.SigningKeyMap()
	if err != nil {
		return nil, err
	}
	a := &Agent{
		cfg: cfg, log: cfg.Logger.With("profile", cfg.Profile.Name),
		keys: keyMap, attempts: map[string]*attempt{}, uploadWait: map[string]chan protocol.UploadURLs{},
		pullWait:      map[string]chan protocol.PullAuth{},
		privateImages: readPrivateImages(cfg.Profile.Dir),
		hbInterval:    15 * time.Second, leaseTTL: 60 * time.Second, wake: make(chan struct{}, 1),
	}
	if err := a.reloadPolicy(true); err != nil {
		return nil, err
	}
	return a, nil
}

func (a *Agent) now() time.Time { return a.cfg.Now() }

func (a *Agent) poke() {
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

// reloadPolicy loads the policy file if it changed. On error the previous
// policy stays in force (or, at start, the error is returned).
func (a *Agent) reloadPolicy(initial bool) error {
	st, err := os.Stat(a.cfg.PolicyPath)
	if err != nil {
		if initial {
			return fmt.Errorf("policy: %w", err)
		}
		a.log.Warn("policy file unreadable; keeping the previous policy", "err", err)
		return nil
	}
	a.mu.Lock()
	unchanged := !initial && st.ModTime().Equal(a.polMod)
	a.mu.Unlock()
	if unchanged {
		return nil
	}
	p, err := policy.Load(a.cfg.PolicyPath)
	if err != nil {
		if initial {
			return err
		}
		a.log.Error("policy file is invalid; keeping the previous policy", "err", err)
		a.mu.Lock()
		a.polMod = st.ModTime()
		a.mu.Unlock()
		return nil
	}
	det := a.cfg.Idle
	if det == nil {
		if det, err = idle.New(p.Idle); err != nil {
			return err
		}
	}
	a.mu.Lock()
	a.pol, a.polMod, a.idleDet = p, st.ModTime(), det
	a.idleChecked = time.Time{}
	a.mu.Unlock()
	if !initial {
		a.log.Info("policy reloaded", "digest", p.Digest, "shares", len(p.Shares))
	}
	return nil
}

func (a *Agent) refreshIdle(force bool) policy.IdleState {
	a.mu.Lock()
	det, last := a.idleDet, a.idleChecked
	a.mu.Unlock()
	if !force && a.now().Sub(last) < ownerCheckInterval && !last.IsZero() {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.idleState
	}
	d, err := det.IdleFor()
	st := policy.IdleState{IdleFor: d, Err: err}
	a.mu.Lock()
	a.idleState, a.idleChecked = st, a.now()
	a.mu.Unlock()
	return st
}

// detect refreshes device facts and runtime tiers.
func (a *Agent) detect(ctx context.Context) error {
	info, err := a.cfg.Runtime.Detect(ctx)
	if err != nil {
		return fmt.Errorf("container runtime unavailable: %w", err)
	}
	if !info.Rootless {
		a.log.Warn("container engine is not rootless; plan §8.3 requires a rootless engine for T1")
	}
	if !info.Apps {
		a.log.Info("apps (jobs with ports) cannot run here: this pool-agent is not a static Linux binary, so it " +
			"cannot reach into an app's container; build it with CGO_ENABLED=0 to allow them")
	}
	si := a.cfg.DeviceInfo()
	gpuTiers := info.GPUTiers
	if len(gpuTiers) == 0 && len(si.GPUs) > 0 {
		// NVIDIA Container Toolkit with CDI gives T1 GPU access. UNTESTED here.
		gpuTiers = []protocol.Tier{protocol.T1}
	}
	a.mu.Lock()
	a.rtInfo = info
	a.os, a.arch = si.OS, si.Arch
	a.dev = policy.Device{
		CPUCores: si.CPUCores, MemoryMB: si.MemoryMB, FreeDiskMB: si.FreeDiskMB, GPUs: si.GPUs,
		Tiers: info.Tiers, GPUTiers: gpuTiers, Apps: info.Apps,
	}
	a.mu.Unlock()
	return nil
}

// Run keeps the session alive until ctx is cancelled.
func (a *Agent) Run(ctx context.Context) error {
	if err := a.detect(ctx); err != nil {
		return err
	}
	// Containers from a previous run cannot be re-attached; remove them.
	if n, err := a.cfg.Runtime.KillAll(ctx, a.cfg.Profile.Name); err == nil && n > 0 {
		a.log.Warn("removed containers left over from a previous run", "count", n)
	}
	a.writeState()
	backoff := time.Second
	for {
		started := a.now()
		err := a.session(ctx)
		if ctx.Err() != nil {
			a.shutdown()
			return nil
		}
		if a.now().Sub(started) > time.Minute {
			backoff = time.Second
		}
		wait := jitter(backoff)
		a.log.Warn("disconnected from server; reconnecting", "err", err, "in", wait.Round(time.Millisecond))
		select {
		case <-ctx.Done():
			a.shutdown()
			return nil
		case <-time.After(wait):
		}
		if backoff < time.Minute {
			backoff *= 2
		}
		// Keep enforcing the kill switch and owner policy while offline.
		a.tick(ctx)
	}
}

func jitter(d time.Duration) time.Duration {
	// ±20 %
	n, _ := rand.Int(rand.Reader, big.NewInt(400))
	return d * time.Duration(800+n.Int64()) / 1000
}

// session runs one WebSocket connection.
func (a *Agent) session(ctx context.Context) error {
	p := a.cfg.Profile
	if !p.CertNotAfter.IsZero() && a.now().After(p.CertNotAfter) {
		return errors.New("device certificate expired; the owner must re-enroll this device (pool-agent enroll)")
	}
	wsURL, err := p.WSURL()
	if err != nil {
		return err
	}
	dialer := a.cfg.Dialer
	if dialer == nil {
		tlsCfg, err := p.TLSConfig()
		if err != nil {
			return err
		}
		dialer = &websocket.Dialer{HandshakeTimeout: 20 * time.Second, TLSClientConfig: tlsCfg, Proxy: http.ProxyFromEnvironment}
	}
	conn, resp, err := dialer.DialContext(ctx, wsURL, nil)
	if err != nil {
		if resp != nil {
			return fmt.Errorf("dial %s: %w (HTTP %s)", wsURL, err, resp.Status)
		}
		return fmt.Errorf("dial %s: %w", wsURL, err)
	}
	defer conn.Close()
	conn.SetReadLimit(maxFrameBytes)

	// 1. challenge
	_ = conn.SetReadDeadline(time.Now().Add(30 * time.Second))
	first, err := readMsg(conn)
	if err != nil {
		return err
	}
	ch, ok := first.(*protocol.Challenge)
	if !ok {
		return fmt.Errorf("expected challenge, got %T", first)
	}
	sig, err := enroll.SignHello(p.Key, ch.NonceB64)
	if err != nil {
		return err
	}
	// 2. hello
	hello := a.buildHello(sig)
	if err := writeRaw(conn, protocol.TypeHello, hello); err != nil {
		return err
	}
	// 3. welcome
	next, err := readMsg(conn)
	if err != nil {
		return err
	}
	if e, ok := next.(*protocol.Error); ok {
		return fmt.Errorf("server refused session: %s: %s", e.Code, e.Message)
	}
	w, ok := next.(*protocol.Welcome)
	if !ok {
		return fmt.Errorf("expected welcome, got %T", next)
	}
	if w.DeviceID != p.DeviceID {
		return fmt.Errorf("welcome is for device %s, not %s", w.DeviceID, p.DeviceID)
	}
	a.mu.Lock()
	if w.HeartbeatIntervalS > 0 {
		a.hbInterval = time.Duration(w.HeartbeatIntervalS) * time.Second
	}
	if w.LeaseTTLS > 0 {
		a.leaseTTL = time.Duration(w.LeaseTTLS) * time.Second
	}
	a.conn = conn
	a.connID++
	a.readyPending = false
	a.nextReadyAt = time.Time{}
	a.renewPending = false
	hb := a.hbInterval
	a.mu.Unlock()
	a.log.Info("connected", "server", p.Server, "device", p.DeviceID)
	defer a.disconnected(conn)
	a.handleStale(w.Stale, "stale at session start")
	a.writeState()
	if err := a.flush(); err != nil {
		return err
	}

	incoming := make(chan any, 64)
	readErr := make(chan error, 1)
	go func() {
		for {
			_ = conn.SetReadDeadline(time.Now().Add(3*hb + 10*time.Second))
			msg, err := readMsg(conn)
			if err != nil {
				readErr <- err
				return
			}
			incoming <- msg
		}
	}()

	hbTicker := time.NewTicker(hb)
	defer hbTicker.Stop()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	if err := a.sendHeartbeat(); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-readErr:
			return err
		case msg := <-incoming:
			if err := a.handle(ctx, msg); err != nil {
				return err
			}
		case <-hbTicker.C:
			if err := a.sendHeartbeat(); err != nil {
				return err
			}
		case <-tick.C:
			a.tick(ctx)
		case <-a.wake:
		}
		if err := a.flush(); err != nil {
			return err
		}
		if err := a.maybeReady(); err != nil {
			return err
		}
	}
}

func (a *Agent) disconnected(conn *websocket.Conn) {
	a.mu.Lock()
	if a.conn == conn {
		a.conn = nil
	}
	waiters := a.uploadWait
	a.uploadWait = map[string]chan protocol.UploadURLs{}
	pulls := a.pullWait
	a.pullWait = map[string]chan protocol.PullAuth{}
	a.mu.Unlock()
	for _, ch := range waiters {
		close(ch)
	}
	for _, ch := range pulls {
		close(ch)
	}
	a.writeState()
}

func readMsg(conn *websocket.Conn) (any, error) {
	kind, raw, err := conn.ReadMessage()
	if err != nil {
		return nil, err
	}
	if kind != websocket.TextMessage {
		return nil, errors.New("unexpected binary frame")
	}
	return protocol.Decode(raw)
}

func writeRaw(conn *websocket.Conn, typ string, msg any) error {
	raw, err := protocol.Encode(typ, msg)
	if err != nil {
		return err
	}
	_ = conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
	return conn.WriteMessage(websocket.TextMessage, raw)
}

// send writes one message now if connected (used for replies that must not
// be buffered, e.g. accept/decline/ready/heartbeat).
func (a *Agent) send(typ string, msg any) error {
	a.mu.Lock()
	conn := a.conn
	a.mu.Unlock()
	if conn == nil {
		return errors.New("not connected")
	}
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	return writeRaw(conn, typ, msg)
}

// enqueue buffers a message that must survive disconnections.
func (a *Agent) enqueue(typ string, msg any, progressBytes int) {
	a.mu.Lock()
	a.outbox = append(a.outbox, outMsg{typ: typ, msg: msg, progress: progressBytes})
	a.outboxBytes += progressBytes
	for a.outboxBytes > maxOutboxProgress {
		dropped := false
		for i, m := range a.outbox {
			if m.progress > 0 {
				a.outboxBytes -= m.progress
				a.outbox = append(a.outbox[:i], a.outbox[i+1:]...)
				dropped = true
				break
			}
		}
		if !dropped {
			break
		}
	}
	a.mu.Unlock()
	a.poke()
}

// flush sends buffered messages and unsent commits.
func (a *Agent) flush() error {
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	for {
		a.mu.Lock()
		conn := a.conn
		if conn == nil || len(a.outbox) == 0 {
			a.mu.Unlock()
			break
		}
		m := a.outbox[0]
		a.mu.Unlock()
		if err := writeRaw(conn, m.typ, m.msg); err != nil {
			return err
		}
		a.mu.Lock()
		if len(a.outbox) > 0 {
			a.outboxBytes -= a.outbox[0].progress
			a.outbox = a.outbox[1:]
		}
		a.mu.Unlock()
	}
	a.mu.Lock()
	conn, connID := a.conn, a.connID
	var commits []*attempt
	for _, at := range a.attempts {
		if at.commit != nil && at.commitSentOn != connID {
			commits = append(commits, at)
		}
	}
	a.mu.Unlock()
	if conn == nil {
		return nil
	}
	for _, at := range commits {
		if err := writeRaw(conn, protocol.TypeCommit, at.commit); err != nil {
			return err
		}
		a.mu.Lock()
		at.commitSentOn = connID
		a.mu.Unlock()
	}
	return nil
}

func (a *Agent) runningList() []protocol.RunningAttempt {
	out := []protocol.RunningAttempt{}
	for _, at := range a.attempts {
		if at.dropped {
			continue
		}
		state := "running"
		if at.state == "assigned" {
			state = "assigned"
		}
		out = append(out, protocol.RunningAttempt{AttemptID: at.id, Epoch: at.epoch, State: state})
	}
	return out
}

func (a *Agent) buildHello(sig string) *protocol.Hello {
	st := a.refreshIdle(true)
	a.mu.Lock()
	defer a.mu.Unlock()
	p := a.cfg.Profile
	return &protocol.Hello{
		Protocol: protocol.Version, AgentVersion: a.cfg.AgentVersion, DeviceID: p.DeviceID, Profile: p.Name,
		CertPEM: string(p.CertPEM), NonceSignatureB64: sig, Capabilities: a.capabilitiesLocked(),
		Shares: a.pol.ShareInfos(a.dev, a.now(), st), PolicyDigest: a.pol.Digest, Running: a.runningList(),
	}
}

func (a *Agent) capabilitiesLocked() protocol.Capabilities {
	si := a.dev
	return protocol.Capabilities{
		OS: a.os, Arch: a.arch, CPUCores: maxInt(si.CPUCores, 1),
		MemoryMB: si.MemoryMB, DiskMB: si.FreeDiskMB, GPUs: si.GPUs,
		Tiers: a.pol.AllowedTiers(si.Tiers), GPUTiers: a.pol.AllowedTiers(si.GPUTiers), AlwaysOn: a.pol.AlwaysOn,
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func (a *Agent) sendHeartbeat() error {
	st := a.refreshIdle(false)
	a.mu.Lock()
	var usage protocol.Usage
	for _, at := range a.attempts {
		if !at.dropped && at.state != "assigned" {
			usage.MemoryMB += at.res.MemoryMB
		}
	}
	hb := &protocol.Heartbeat{
		Running: a.runningList(), Usage: usage, OwnerActive: st.Err != nil || st.IdleFor < time.Minute,
		Shares: a.pol.ShareInfos(a.dev, a.now(), st),
	}
	a.mu.Unlock()
	return a.send(protocol.TypeHeartbeat, hb)
}

// usage returns per-group usage of this profile and device-wide usage.
func (a *Agent) usageLocked() (map[string]policy.Used, policy.Used) {
	by := map[string]policy.Used{}
	for _, at := range a.attempts {
		if at.dropped {
			continue
		}
		by[at.group] = by[at.group].Add(at.res)
	}
	return by, a.cfg.Capacity.Total()
}

func (a *Agent) maybeReady() error {
	st := a.refreshIdle(false)
	a.mu.Lock()
	now := a.now()
	if a.conn == nil || a.killed {
		a.mu.Unlock()
		return nil
	}
	if a.readyPending && now.Sub(a.readySentAt) > readyTimeout {
		a.readyPending = false
	}
	if a.readyPending || now.Before(a.nextReadyAt) {
		a.mu.Unlock()
		return nil
	}
	by, total := a.usageLocked()
	free, ok := a.pol.Free(a.dev, by, total, now, st)
	tiers := a.pol.AllowedTiers(a.dev.Tiers)
	if !ok || len(tiers) == 0 || free.CPU < 0.1 || free.MemoryMB < 16 {
		a.nextReadyAt = now.Add(5 * time.Second)
		a.mu.Unlock()
		return nil
	}
	a.readyPending, a.readySentAt = true, now
	images := append([]string(nil), a.recentImages...)
	a.mu.Unlock()
	return a.send(protocol.TypeReady, &protocol.Ready{Free: free, Tiers: tiers, Images: images})
}

const maxRecentImages = 100

var digestRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// rememberImage notes that this computer has an image (by digest) after running it.
func (a *Agent) rememberImage(image string) {
	_, digest, ok := strings.Cut(image, "@")
	if !ok || !digestRE.MatchString(digest) { // the server's own check: one bad entry must not spoil Ready
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for i, d := range a.recentImages {
		if d == digest {
			a.recentImages = append(a.recentImages[:i], a.recentImages[i+1:]...)
			break
		}
	}
	a.recentImages = append(a.recentImages, digest)
	if len(a.recentImages) > maxRecentImages {
		a.recentImages = a.recentImages[len(a.recentImages)-maxRecentImages:]
	}
}

// handle dispatches one server message.
func (a *Agent) handle(ctx context.Context, msg any) error {
	switch m := msg.(type) {
	case *protocol.HeartbeatAck:
		a.handleStale(m.Stale, "lease no longer valid")
	case *protocol.Offer:
		return a.handleOffer(ctx, m)
	case *protocol.NoWork:
		a.mu.Lock()
		a.readyPending = false
		retry := time.Duration(m.RetryAfterS) * time.Second
		if retry <= 0 {
			retry = 5 * time.Second
		}
		a.nextReadyAt = a.now().Add(retry)
		a.mu.Unlock()
	case *protocol.UploadURLs:
		a.mu.Lock()
		ch := a.uploadWait[m.AttemptID]
		delete(a.uploadWait, m.AttemptID)
		a.mu.Unlock()
		if ch != nil {
			ch <- *m
			close(ch)
		}
	case *protocol.PullAuth:
		a.mu.Lock()
		ch := a.pullWait[m.AttemptID]
		delete(a.pullWait, m.AttemptID)
		a.mu.Unlock()
		if ch != nil {
			ch <- *m
			close(ch)
		}
	case *protocol.CommitAck:
		a.handleCommitAck(m)
	case *protocol.CertRenewed:
		a.mu.Lock()
		a.renewPending = false
		a.mu.Unlock()
		notAfter, err := a.cfg.Profile.UpdateCert([]byte(m.CertPEM))
		if err != nil {
			a.log.Error("renewed certificate rejected", "err", err)
		} else {
			a.log.Info("device certificate renewed", "not_after", notAfter)
		}
	case *protocol.Cancel:
		a.dropAttempt(m.AttemptID, "cancelled by server: "+m.Reason)
	case *protocol.Preempt:
		a.dropAttempt(m.AttemptID, "preempted by server: "+m.Reason)
	case *protocol.Revoke:
		a.mu.Lock()
		var ids []string
		for id, at := range a.attempts {
			if at.group == m.Group {
				ids = append(ids, id)
			}
		}
		a.mu.Unlock()
		for _, id := range ids {
			a.dropAttempt(id, "group revoked: "+m.Reason)
		}
	case *protocol.TunnelOpen:
		go a.openTunnel(ctx, m)
	case *protocol.Error:
		a.log.Warn("server error", "code", m.Code, "message", m.Message)
	case *protocol.Challenge, *protocol.Welcome:
		a.log.Warn("unexpected session message mid-session")
	default:
		a.log.Warn("ignoring message from server", "type", fmt.Sprintf("%T", msg))
	}
	return nil
}

func (a *Agent) handleStale(ids []string, why string) {
	for _, id := range ids {
		a.dropAttempt(id, why)
	}
}

// dropAttempt stops an attempt the server no longer wants. Nothing is
// committed for it.
func (a *Agent) dropAttempt(id, why string) {
	a.mu.Lock()
	at := a.attempts[id]
	if at == nil || at.dropped {
		a.mu.Unlock()
		return
	}
	at.dropped = true
	cancel := at.cancel
	finished := at.state == "finishing"
	a.mu.Unlock()
	a.log.Info("stopping attempt", "attempt", id, "why", why)
	if cancel != nil {
		cancel()
	}
	if finished {
		a.finishAttempt(id)
	}
}

func (a *Agent) handleCommitAck(m *protocol.CommitAck) {
	a.mu.Lock()
	at := a.attempts[m.AttemptID]
	a.mu.Unlock()
	if at == nil {
		return
	}
	if m.Accepted {
		a.log.Info("result committed", "attempt", m.AttemptID)
	} else {
		reason := ""
		if m.Reason != nil {
			reason = *m.Reason
		}
		a.log.Warn("result rejected by server", "attempt", m.AttemptID, "reason", reason)
	}
	a.finishAttempt(m.AttemptID)
}

// finishAttempt removes a finished attempt and its files.
func (a *Agent) finishAttempt(id string) {
	a.mu.Lock()
	at := a.attempts[id]
	delete(a.attempts, id)
	a.mu.Unlock()
	if at == nil {
		return
	}
	a.cfg.Capacity.Release(a.cfg.Profile.Name + "/" + id)
	if err := a.cfg.Runtime.Cleanup(id); err != nil {
		a.log.Warn("cleanup failed", "attempt", id, "err", err)
	}
	a.writeState()
	a.poke()
}

func (a *Agent) decline(o *protocol.Offer, reason, detail string) error {
	a.log.Info("declined offer", "attempt", o.AttemptID, "reason", reason, "detail", detail)
	return a.send(protocol.TypeDecline, &protocol.Decline{AttemptID: o.AttemptID, Epoch: o.Epoch, Reason: reason, Detail: detail})
}

func (a *Agent) handleOffer(ctx context.Context, o *protocol.Offer) error {
	a.mu.Lock()
	a.readyPending = false
	a.nextReadyAt = time.Time{}
	killed := a.killed
	_, dup := a.attempts[o.AttemptID]
	a.mu.Unlock()
	if dup {
		return nil
	}
	if killed {
		return a.decline(o, protocol.DeclineKillSwitch, "kill switch is on")
	}
	payload, err := protocol.VerifyEnvelope(o.Envelope, a.keys)
	if err != nil {
		return a.decline(o, protocol.DeclineSignatureInvalid, err.Error())
	}
	var env protocol.TaskEnvelope
	if err := json.Unmarshal(payload, &env); err != nil {
		return a.decline(o, protocol.DeclineOther, "envelope payload is not valid JSON")
	}
	if env.V != protocol.Version || env.AttemptID != o.AttemptID || env.TaskID != o.TaskID || env.Epoch != o.Epoch {
		return a.decline(o, protocol.DeclineOther, "envelope does not match the offer")
	}
	if env.DeviceID != a.cfg.Profile.DeviceID {
		return a.decline(o, protocol.DeclineWrongDevice, "envelope is for another device")
	}
	now := a.now()
	if !now.Before(env.ExpiresAt) || (!o.ExpiresAt.IsZero() && !now.Before(o.ExpiresAt)) {
		return a.decline(o, protocol.DeclineEnvelopeExpired, "offer expired")
	}
	st := a.refreshIdle(false)
	a.mu.Lock()
	by, total := a.usageLocked()
	d := a.pol.Admit(&env, a.dev, by, total, now, st)
	a.mu.Unlock()
	if !d.OK {
		return a.decline(o, d.Reason, d.Detail)
	}
	if !a.cfg.Capacity.Reserve(a.cfg.Profile.Name+"/"+env.AttemptID, env.Spec.Resources, a.devCopy()) {
		return a.decline(o, protocol.DeclineBusy, "device capacity is in use by another profile")
	}
	runCtx, cancel := context.WithCancel(context.Background())
	at := &attempt{
		id: env.AttemptID, taskID: env.TaskID, epoch: env.Epoch, group: env.Group, submitter: env.Submitter,
		image: env.Spec.Image, tier: d.Tier, res: env.Spec.Resources, env: &env, state: "assigned", cancel: cancel,
	}
	a.mu.Lock()
	a.attempts[at.id] = at
	a.mu.Unlock()
	if err := a.send(protocol.TypeAccept, &protocol.Accept{AttemptID: at.id, Epoch: at.epoch, Tier: d.Tier}); err != nil {
		cancel()
		a.mu.Lock()
		delete(a.attempts, at.id)
		a.mu.Unlock()
		a.cfg.Capacity.Release(a.cfg.Profile.Name + "/" + at.id)
		return err
	}
	a.log.Info("accepted task", "attempt", at.id, "group", at.group, "submitter", at.submitter, "tier", d.Tier)
	a.writeState()
	go a.runAttempt(runCtx, at)
	return nil
}

func (a *Agent) devCopy() policy.Device {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.dev
}

// tick enforces the kill switch, owner-return actions, policy reloads and
// certificate renewal.
func (a *Agent) tick(ctx context.Context) {
	a.checkKillSwitch(ctx)
	_ = a.reloadPolicy(false)
	a.checkOwner(ctx)
	a.checkRenewal()
}

func (a *Agent) killFile() string { return filepath.Join(a.cfg.Home, enroll.KillFile) }

func (a *Agent) checkKillSwitch(ctx context.Context) {
	_, err := os.Stat(a.killFile())
	on := err == nil
	a.mu.Lock()
	was := a.killed
	a.killed = on
	var victims []*attempt
	if on {
		for _, at := range a.attempts {
			if !at.dropped && at.stopReason == "" && at.state != "finishing" {
				at.stopReason = protocol.AbortKillSwitch
				victims = append(victims, at)
			}
		}
	}
	a.mu.Unlock()
	if on && !was {
		a.log.Warn("kill switch is on: stopping all work", "file", a.killFile())
	}
	if !on && was {
		a.log.Info("kill switch released: accepting work again")
	}
	for _, at := range victims {
		at.cancel()
	}
	if on && (len(victims) > 0 || !was) {
		_, _ = a.cfg.Runtime.KillAll(ctx, a.cfg.Profile.Name)
	}
	if on != was {
		a.writeState()
	}
}

func (a *Agent) checkOwner(ctx context.Context) {
	st := a.refreshIdle(false)
	type action struct {
		at   *attempt
		what string
	}
	var actions []action
	a.mu.Lock()
	now := a.now()
	for _, at := range a.attempts {
		if at.dropped || at.stopReason != "" || at.state != "running" {
			continue
		}
		share := a.pol.Find(at.group)
		if share == nil {
			at.stopReason = protocol.AbortPolicy
			actions = append(actions, action{at, "kill"})
			continue
		}
		if at.env != nil && len(at.env.Spec.Ports) > 0 && (!share.Apps.Allow || !a.dev.Apps ||
			float64(at.env.Spec.MaxRuntimeS) > share.Apps.MaxHours*3600) {
			// The owner turned apps off, or now allows them for less time than this one may run.
			at.stopReason = protocol.AbortPolicy
			actions = append(actions, action{at, "kill"})
			continue
		}
		active, _ := share.Active(now, st)
		switch {
		case !active && at.ownerAction == "":
			switch share.OnOwnerReturn {
			case policy.ReturnPause:
				at.ownerAction = "paused"
				actions = append(actions, action{at, "pause"})
			case policy.ReturnThrottle:
				at.ownerAction = "throttled"
				actions = append(actions, action{at, "throttle"})
			default:
				at.ownerAction = "stopping"
				at.stopReason = protocol.AbortOwnerReturned
				actions = append(actions, action{at, "stop"})
			}
		case active && at.ownerAction == "paused":
			at.ownerAction = ""
			actions = append(actions, action{at, "unpause"})
		case active && at.ownerAction == "throttled":
			at.ownerAction = ""
			actions = append(actions, action{at, "unthrottle"})
		}
	}
	a.mu.Unlock()
	for _, act := range actions {
		var err error
		switch act.what {
		case "kill":
			act.at.cancel()
		case "pause":
			err = a.cfg.Runtime.Pause(ctx, act.at.id)
		case "unpause":
			err = a.cfg.Runtime.Unpause(ctx, act.at.id)
		case "throttle":
			err = a.cfg.Runtime.Throttle(ctx, act.at.id, act.at.res.CPU/4)
		case "unthrottle":
			err = a.cfg.Runtime.Throttle(ctx, act.at.id, act.at.res.CPU)
		case "stop":
			// SIGTERM gives the job the grace period to checkpoint (plan §7.5).
			go func(id string) { _ = a.cfg.Runtime.Stop(context.Background(), id, stopGrace) }(act.at.id)
		}
		a.log.Info("owner policy action", "attempt", act.at.id, "action", act.what, "err", err)
	}
}

func (a *Agent) checkRenewal() {
	p := a.cfg.Profile
	a.mu.Lock()
	if a.conn == nil || (a.renewPending && a.now().Sub(a.renewSentAt) < 5*time.Minute) || !p.NeedsRenewal(a.now()) {
		a.mu.Unlock()
		return
	}
	a.renewPending, a.renewSentAt = true, a.now()
	a.mu.Unlock()
	csr, err := enroll.MakeCSR(p.Key, p.DeviceID)
	if err != nil {
		a.log.Error("cannot build renewal CSR", "err", err)
		return
	}
	if err := a.send(protocol.TypeRenewCert, &protocol.RenewCert{CSRPEM: csr}); err != nil {
		a.log.Warn("renewal request not sent", "err", err)
	}
}

// shutdown stops all containers (they cannot be re-attached after a
// restart) and tells the server, best effort.
func (a *Agent) shutdown() {
	a.mu.Lock()
	var running []*attempt
	for _, at := range a.attempts {
		if !at.dropped && at.state != "finishing" {
			if at.stopReason == "" {
				at.stopReason = protocol.AbortAgentShutdown
			}
			running = append(running, at)
		}
	}
	a.mu.Unlock()
	for _, at := range running {
		at.cancel()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_, _ = a.cfg.Runtime.KillAll(ctx, a.cfg.Profile.Name)
	a.writeState()
}

const privateImagesFile = "private-images.json"

func readPrivateImages(dir string) map[string]string {
	out := map[string]string{}
	raw, err := os.ReadFile(filepath.Join(dir, privateImagesFile))
	if err != nil {
		return out
	}
	_ = json.Unmarshal(raw, &out)
	return out
}

func (a *Agent) setImageState(image, state string) {
	a.privateImages[image] = state
	raw, _ := json.Marshal(a.privateImages)
	path := filepath.Join(a.cfg.Profile.Dir, privateImagesFile)
	if err := os.WriteFile(path+".tmp", raw, 0o600); err == nil {
		_ = os.Rename(path+".tmp", path)
	}
}

// markPrivate records that image was pulled with a token, so another group
// must pull it itself (without credentials) before using it here. Images
// already found to be public stay public.
func (a *Agent) markPrivate(image string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.privateImages[image] == "" {
		a.setImageState(image, "token")
	}
}

// markPublic records that image can be pulled without credentials.
func (a *Agent) markPublic(image string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.privateImages[image] != "public" {
		a.setImageState(image, "public")
	}
}

func (a *Agent) isPrivate(image string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.privateImages[image] == "token"
}

// openTunnel connects one connection to an app's port (pool-run step 2): the server's tunnel endpoint on one
// side, `docker exec ... pool-agent pipe 127.0.0.1:PORT` in the app's container on the other. Only ports the
// signed task lists, and only for an attempt that is running here.
func (a *Agent) openTunnel(ctx context.Context, m *protocol.TunnelOpen) {
	a.mu.Lock()
	at := a.attempts[m.AttemptID]
	ok := at != nil && at.state == "running" && at.stopReason == "" && at.env != nil &&
		slices.Contains(at.env.Spec.Ports, m.Port)
	if ok { // the owner may have turned apps off since it started (checkOwner stops it soon)
		share := a.pol.Find(at.group)
		ok = share != nil && share.Apps.Allow && a.dev.Apps
	}
	full := a.tunnels >= maxTunnels
	if ok && !full {
		a.tunnels++
	}
	a.mu.Unlock()
	if !ok {
		a.log.Warn("tunnel refused: no such running app port here", "attempt", m.AttemptID, "port", m.Port)
		return
	}
	if full {
		a.log.Warn("tunnel refused: too many app connections open on this computer", "max", maxTunnels)
		return
	}
	defer func() {
		a.mu.Lock()
		a.tunnels--
		a.mu.Unlock()
	}()
	u, err := a.tunnelURL(m.TunnelID)
	if err != nil {
		a.log.Warn("tunnel", "err", err)
		return
	}
	dialer := a.cfg.Dialer
	if dialer == nil {
		tlsCfg, err := a.cfg.Profile.TLSConfig()
		if err != nil {
			a.log.Warn("tunnel", "err", err)
			return
		}
		dialer = &websocket.Dialer{HandshakeTimeout: 20 * time.Second, TLSClientConfig: tlsCfg, Proxy: http.ProxyFromEnvironment}
	}
	// The one-time secret goes in a header, not the URL: servers and proxies log URLs.
	ws, _, err := dialer.DialContext(ctx, u, http.Header{tunnelSecretHeader: {m.Secret}})
	if err != nil {
		a.log.Warn("tunnel: cannot reach the server", "err", err)
		return
	}
	defer ws.Close()
	ws.SetReadLimit(maxFrameBytes)
	pipe, err := a.cfg.Runtime.Tunnel(ctx, m.AttemptID, m.Port)
	if err != nil {
		a.log.Warn("tunnel: cannot reach the app", "err", err)
		_ = ws.WriteMessage(websocket.CloseMessage, websocket.FormatCloseMessage(1011, "the app cannot be reached"))
		return
	}
	defer pipe.Close()
	go func() { // app -> server: the only writer on ws
		buf := make([]byte, 32*1024)
		for {
			n, err := pipe.Read(buf)
			if n > 0 {
				if werr := ws.WriteMessage(websocket.BinaryMessage, buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				_ = ws.WriteMessage(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
				return
			}
		}
	}()
	for { // server -> app: the only reader on ws
		typ, data, err := ws.ReadMessage()
		if err != nil {
			return
		}
		if typ == websocket.BinaryMessage {
			if _, err := pipe.Write(data); err != nil {
				return
			}
		}
	}
}

const (
	// tunnelSecretHeader carries a tunnel's one-time secret to the server.
	tunnelSecretHeader = "X-Pool-Tunnel-Secret"
	// maxTunnels is the most app connections open at once on this computer. Each one is a `docker exec`
	// process here, outside the share's limits (security review of pool-run step 2).
	maxTunnels = 32
)

// tunnelURL is the server's agent tunnel endpoint for one tunnel.
func (a *Agent) tunnelURL(id string) (string, error) {
	base, err := a.cfg.Profile.WSURL()
	if err != nil {
		return "", err
	}
	u, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	u.Path = "/api/v1/agent/tunnels/" + url.PathEscape(id)
	u.RawQuery = ""
	return u.String(), nil
}
