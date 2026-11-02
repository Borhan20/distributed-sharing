// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package policy

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"pool/agent/protocol"
)

// planExample is the policy from plan §6.4 (+ fields from the design doc §7).
const planExample = `
shares:
  - group: research-team
    when:
      weekdays: "19:00-08:00"
      weekends: all-day
      only_when_idle: 10m
    limits: { cpu_percent: 75, memory: 24Gi, gpu: true, disk: 100Gi }
    accept_from: all-members
    on_owner_return: checkpoint-and-stop
  - group: uni-ml-lab
    when: { only_when_idle: 15m }
    limits: { cpu_percent: 50, memory: 8Gi, gpu: false, disk: 20Gi }
    accept_from: [alice, bob]
    accept_new_members: true
    on_owner_return: pause
priority: [uni-ml-lab, research-team]
isolation:
  allow_tiers: [T1, T2, T3]
egress: deny
kill_switch: true
`

var (
	// 2026-11-02 is a Monday; 2026-11-07 is a Saturday. Local time zone.
	monNoon  = time.Date(2026, 11, 2, 12, 0, 0, 0, time.Local)
	monNight = time.Date(2026, 11, 2, 22, 0, 0, 0, time.Local)
	monEarly = time.Date(2026, 11, 2, 7, 59, 0, 0, time.Local)
	mon8am   = time.Date(2026, 11, 2, 8, 0, 0, 0, time.Local)
	satNoon  = time.Date(2026, 11, 7, 12, 0, 0, 0, time.Local)
	idle1h   = IdleState{IdleFor: time.Hour}
	active   = IdleState{IdleFor: 0}
)

func mustParse(t *testing.T, y string) *Policy {
	t.Helper()
	p, err := Parse([]byte(y))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return p
}

func dev() Device {
	return Device{
		CPUCores: 16, MemoryMB: 65536, FreeDiskMB: 500000,
		GPUs:  []protocol.GPUInfo{{Vendor: "nvidia", Model: "test", MemoryMB: 24576}},
		Tiers: []protocol.Tier{protocol.T1, protocol.T2}, GPUTiers: []protocol.Tier{protocol.T1},
	}
}

func env(group, submitter string, mutate func(*protocol.TaskSpec)) *protocol.TaskEnvelope {
	e := &protocol.TaskEnvelope{
		Group: group, Submitter: submitter, Epoch: 1,
		Spec: protocol.TaskSpec{
			Image:     "registry.example.org/research/probe@sha256:" + strings.Repeat("b", 64),
			Command:   []string{"true"},
			Resources: protocol.TaskResources{CPU: 4, MemoryMB: 4096, DiskMB: 1024},
			MinTier:   protocol.T1, Network: protocol.TaskNetwork{Egress: "none"}, MaxRuntimeS: 60,
		},
	}
	if mutate != nil {
		mutate(&e.Spec)
	}
	return e
}

func TestParsePlanExample(t *testing.T) {
	p := mustParse(t, planExample)
	if len(p.Shares) != 2 || !strings.HasPrefix(p.Digest, "sha256:") || len(p.Digest) != 71 {
		t.Fatalf("policy: %+v", p)
	}
	rt := p.Find("research-team")
	if rt.Limits.MemoryMB() != 24576 || rt.Limits.DiskMB() != 102400 || rt.When.IdleRequired() != 10*time.Minute {
		t.Fatalf("limits: %+v", rt.Limits)
	}
	if !rt.AcceptFrom.All || p.Find("uni-ml-lab").AcceptFrom.Handles[1] != "bob" {
		t.Fatal("accept_from")
	}
	order := p.Ordered()
	if order[0].Group != "uni-ml-lab" || order[1].Group != "research-team" {
		t.Fatalf("priority order wrong: %s, %s", order[0].Group, order[1].Group)
	}
	shares := p.ShareInfos(dev(), monNight, idle1h)
	if len(shares) != 2 || shares[0].Group != "uni-ml-lab" || !shares[0].ActiveNow || !shares[1].ActiveNow {
		t.Fatalf("shares: %+v", shares)
	}
	if shares[1].Limits.MemoryMB != 24576 || !shares[1].Limits.GPU || shares[0].Limits.GPU {
		t.Fatalf("share limits: %+v", shares)
	}
}

func TestDefaultPolicySharesNothing(t *testing.T) {
	p := mustParse(t, DefaultYAML)
	if len(p.Shares) != 0 || p.Egress != "deny" || p.Idle != "auto" {
		t.Fatalf("default: %+v", p)
	}
	d := p.Admit(env("research-team", "alice", nil), dev(), nil, Used{}, monNight, idle1h)
	if d.OK || d.Reason != protocol.DeclineGroupNotShared {
		t.Fatalf("default policy admitted work: %+v", d)
	}
	if _, ok := p.Free(dev(), nil, Used{}, monNight, idle1h); ok {
		t.Fatal("default policy should not advertise capacity")
	}
	path := filepath.Join(t.TempDir(), "p", "policy.yaml")
	if wrote, err := WriteDefault(path); err != nil || !wrote {
		t.Fatalf("WriteDefault: %v %v", wrote, err)
	}
	_ = os.WriteFile(path, []byte("shares: []\n# edited by owner\n"), 0o600)
	if wrote, _ := WriteDefault(path); wrote {
		t.Fatal("WriteDefault overwrote the owner's file")
	}
	st, _ := os.Stat(path)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode().Perm())
	}
	if p, err := Parse([]byte("# only comments\n")); err != nil || len(p.Shares) != 0 {
		t.Fatalf("empty policy: %v", err)
	}
}

func TestWindows(t *testing.T) {
	w := mustParse(t, planExample).Find("research-team").When
	cases := []struct {
		at   time.Time
		want bool
	}{
		{monNoon, false}, {monNight, true}, {monEarly, true}, {mon8am, false}, {satNoon, true},
		{time.Date(2026, 11, 2, 19, 0, 0, 0, time.Local), true},
		{time.Date(2026, 11, 2, 18, 59, 0, 0, time.Local), false},
	}
	for _, c := range cases {
		if got := w.ActiveAt(c.at); got != c.want {
			t.Errorf("ActiveAt(%s) = %v, want %v", c.at.Format("Mon 15:04"), got, c.want)
		}
	}
	only := mustParse(t, `
shares:
  - group: g1
    when: { weekdays: "09:00-12:00,13:00-17:00" }
    limits: { cpu_percent: 50, memory: 1Gi, disk: 1Gi }
    accept_from: all-members
`).Find("g1").When
	for at, want := range map[time.Time]bool{
		time.Date(2026, 11, 2, 10, 0, 0, 0, time.Local):  true,
		time.Date(2026, 11, 2, 12, 30, 0, 0, time.Local): false,
		time.Date(2026, 11, 2, 16, 59, 0, 0, time.Local): true,
		satNoon: false, // weekends not set -> never
	} {
		if got := only.ActiveAt(at); got != want {
			t.Errorf("multi-range ActiveAt(%s) = %v", at.Format("Mon 15:04"), got)
		}
	}
	for _, bad := range []string{"25:00-26:00", "9-17", "09:00-09:00", "09:0-10:00", "sometimes"} {
		if _, err := ParseWindow(bad); err == nil {
			t.Errorf("ParseWindow(%q) accepted", bad)
		}
	}
	if w, err := ParseWindow("never"); err != nil || w.contains(600) {
		t.Error("never")
	}
}

func TestAdmit(t *testing.T) {
	p := mustParse(t, planExample)
	gpuEnv := env("research-team", "alice", func(s *protocol.TaskSpec) {
		s.Resources.GPUs = 1
		v := "nvidia"
		s.Resources.GPUVendor = &v
		s.Resources.GPUMemoryMB = 8192
	})
	cases := []struct {
		name   string
		env    *protocol.TaskEnvelope
		at     time.Time
		idle   IdleState
		group  Used
		total  Used
		reason string
		tier   protocol.Tier
	}{
		{name: "ok picks strongest tier", env: env("research-team", "alice", nil), at: monNight, idle: idle1h, tier: protocol.T2},
		{name: "gpu job uses gpu tier", env: gpuEnv, at: monNight, idle: idle1h, tier: protocol.T1},
		{name: "min tier T2 ok", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.MinTier = protocol.T2 }), at: monNight, idle: idle1h, tier: protocol.T2},
		{name: "min tier T3 unavailable", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.MinTier = protocol.T3 }), at: monNight, idle: idle1h, reason: protocol.DeclineTierNotAllowed},
		{name: "gpu job needing T2", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.Resources.GPUs = 1; s.MinTier = protocol.T2 }), at: monNight, idle: idle1h, reason: protocol.DeclineTierNotAllowed},
		{name: "unknown group", env: env("other-group", "alice", nil), at: monNight, idle: idle1h, reason: protocol.DeclineGroupNotShared},
		{name: "outside window", env: env("research-team", "alice", nil), at: monNoon, idle: idle1h, reason: protocol.DeclineOutsideWindow},
		{name: "owner active", env: env("research-team", "alice", nil), at: monNight, idle: active, reason: protocol.DeclineOwnerActive},
		{name: "idle unknown counts as active", env: env("research-team", "alice", nil), at: monNight, idle: IdleState{IdleFor: time.Hour, Err: os.ErrNotExist}, reason: protocol.DeclineOwnerActive},
		{name: "idle too short", env: env("research-team", "alice", nil), at: monNight, idle: IdleState{IdleFor: 9 * time.Minute}, reason: protocol.DeclineOwnerActive},
		{name: "submitter not accepted", env: env("uni-ml-lab", "mallory", nil), at: monNoon, idle: idle1h, reason: protocol.DeclineSubmitterNotAccepted},
		{name: "listed submitter ok", env: env("uni-ml-lab", "bob", nil), at: monNoon, idle: idle1h, tier: protocol.T2},
		{name: "probation refused", env: func() *protocol.TaskEnvelope {
			e := env("research-team", "alice", nil)
			e.SubmitterProbation = true
			return e
		}(), at: monNight, idle: idle1h, reason: protocol.DeclineProbationNotAccepted},
		{name: "probation accepted by share", env: func() *protocol.TaskEnvelope {
			e := env("uni-ml-lab", "alice", nil)
			e.SubmitterProbation = true
			return e
		}(), at: monNoon, idle: idle1h, tier: protocol.T2},
		{name: "unpinned image", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.Image = "python:3.12" }), at: monNight, idle: idle1h, reason: protocol.DeclineImageNotAllowed},
		{name: "egress declined", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.Network.Egress = "allowlist" }), at: monNight, idle: idle1h, reason: protocol.DeclineEgressNotAllowed},
		{name: "cpu over share limit", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.Resources.CPU = 13 }), at: monNight, idle: idle1h, reason: protocol.DeclineResourcesExceedLimits},
		{name: "cpu limit counts group usage", env: env("research-team", "alice", nil), at: monNight, idle: idle1h, group: Used{CPU: 9}, total: Used{CPU: 9}, reason: protocol.DeclineResourcesExceedLimits},
		{name: "device cpu exhausted by other group", env: env("research-team", "alice", nil), at: monNight, idle: idle1h, total: Used{CPU: 14}, reason: protocol.DeclineResourcesExceedLimits},
		{name: "memory over share limit", env: env("uni-ml-lab", "alice", func(s *protocol.TaskSpec) { s.Resources.MemoryMB = 9000 }), at: monNoon, idle: idle1h, reason: protocol.DeclineResourcesExceedLimits},
		{name: "disk over share limit", env: env("uni-ml-lab", "alice", func(s *protocol.TaskSpec) { s.Resources.DiskMB = 30000 }), at: monNoon, idle: idle1h, reason: protocol.DeclineResourcesExceedLimits},
		{name: "gpu not allowed by share", env: env("uni-ml-lab", "alice", func(s *protocol.TaskSpec) { s.Resources.GPUs = 1 }), at: monNoon, idle: idle1h, reason: protocol.DeclineResourcesExceedLimits},
		{name: "gpu busy", env: gpuEnv, at: monNight, idle: idle1h, total: Used{GPUs: 1}, reason: protocol.DeclineResourcesExceedLimits},
		{name: "gpu memory too small", env: env("research-team", "alice", func(s *protocol.TaskSpec) { s.Resources.GPUs = 1; s.Resources.GPUMemoryMB = 40000 }), at: monNight, idle: idle1h, reason: protocol.DeclineResourcesExceedLimits},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			d := p.Admit(c.env, dev(), map[string]Used{c.env.Group: c.group}, c.total, c.at, c.idle)
			if c.reason == "" {
				if !d.OK || d.Tier != c.tier {
					t.Fatalf("want OK tier %s, got %+v", c.tier, d)
				}
				return
			}
			if d.OK || d.Reason != c.reason {
				t.Fatalf("want %s, got %+v", c.reason, d)
			}
		})
	}
}

func TestAdmitRespectsAllowTiersAndImageAllowlist(t *testing.T) {
	p := mustParse(t, strings.Replace(planExample, "allow_tiers: [T1, T2, T3]", "allow_tiers: [T1]", 1)+
		"images:\n  allow: [registry.example.org/research/]\n")
	d := p.Admit(env("research-team", "alice", nil), dev(), nil, Used{}, monNight, idle1h)
	if !d.OK || d.Tier != protocol.T1 {
		t.Fatalf("allow_tiers [T1] should force T1: %+v", d)
	}
	d = p.Admit(env("research-team", "alice", func(s *protocol.TaskSpec) {
		s.Image = "registry.example.org/other/x@sha256:" + strings.Repeat("c", 64)
	}), dev(), nil, Used{}, monNight, idle1h)
	if d.OK || d.Reason != protocol.DeclineImageNotAllowed {
		t.Fatalf("image allowlist: %+v", d)
	}
}

func TestFree(t *testing.T) {
	p := mustParse(t, planExample)
	free, ok := p.Free(dev(), map[string]Used{"research-team": {CPU: 4, MemoryMB: 4096}}, Used{CPU: 4, MemoryMB: 4096}, monNight, idle1h)
	if !ok || free.CPU != 8 || free.MemoryMB != 20480 || free.GPUs != 1 {
		t.Fatalf("free: %+v", free)
	}
	if _, ok := p.Free(dev(), nil, Used{}, monNight, active); ok {
		t.Fatal("no share should be active while the owner is active")
	}
}

func TestParseRejects(t *testing.T) {
	base := `
shares:
  - group: g1
    limits: { cpu_percent: 50, memory: 1Gi, disk: 1Gi }
    accept_from: all-members
`
	bad := map[string]string{
		"unknown key":       base + "colour: blue\n",
		"typo in share":     strings.Replace(base, "accept_from", "acept_from", 1),
		"cpu over 100":      strings.Replace(base, "cpu_percent: 50", "cpu_percent: 150", 1),
		"bad memory":        strings.Replace(base, "memory: 1Gi", "memory: lots", 1),
		"missing disk":      strings.Replace(base, ", disk: 1Gi", "", 1),
		"missing accept":    strings.Replace(base, "    accept_from: all-members\n", "", 1),
		"bad accept":        strings.Replace(base, "all-members", "everyone", 1),
		"bad group":         strings.Replace(base, "group: g1", "group: G_1", 1),
		"duplicate":         base + "  - group: g1\n    limits: { cpu_percent: 5, memory: 1Gi, disk: 1Gi }\n    accept_from: all-members\n",
		"priority unknown":  base + "priority: [g2]\n",
		"bad tier":          base + "isolation: { allow_tiers: [T9] }\n",
		"bad egress":        base + "egress: maybe\n",
		"bad idle mode":     base + "idle: sometimes\n",
		"bad return action": strings.Replace(base, "accept_from: all-members", "accept_from: all-members\n    on_owner_return: explode", 1),
		"bad idle duration": strings.Replace(base, "accept_from: all-members", "accept_from: all-members\n    when: { only_when_idle: soon }", 1),
	}
	for name, y := range bad {
		if _, err := Parse([]byte(y)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := Parse([]byte(base)); err != nil {
		t.Fatalf("base policy rejected: %v", err)
	}
}

func TestDigestChangesWithContent(t *testing.T) {
	a := mustParse(t, planExample)
	b := mustParse(t, planExample+"\n# comment\n")
	if a.Digest == b.Digest {
		t.Fatal("digest should cover the file bytes")
	}
}

func TestParseQuantity(t *testing.T) {
	for in, want := range map[string]int64{"24Gi": 24576, "512Mi": 512, "100": 100, "1Ti": 1048576, "1G": 954} {
		got, err := ParseQuantityMB(in)
		if err != nil || got != want {
			t.Errorf("ParseQuantityMB(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
}
