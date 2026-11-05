package policy

import (
	"strings"
	"testing"
)

func ptr[T any](v T) *T { return &v }

func parseEdited(t *testing.T, raw []byte) *Policy {
	t.Helper()
	p, err := Parse(raw)
	if err != nil {
		t.Fatalf("result does not parse: %v\n%s", err, raw)
	}
	return p
}

func TestAddShareToDefaultKeepsCommentsAndDefaults(t *testing.T) {
	out, created, err := AddOrUpdateShare([]byte(DefaultYAML), ShareEdit{Group: "ml-test-lab"},
		ShareDefaults{TotalMemoryMB: 15 * 1024})
	if err != nil {
		t.Fatal(err)
	}
	if !created {
		t.Fatal("expected a new share")
	}
	for _, keep := range []string{"# The server can never widen this policy", "# T0 (bare metal) is off by default",
		"# jobs get no internet access"} {
		if !strings.Contains(string(out), keep) {
			t.Errorf("comment %q was lost:\n%s", keep, out)
		}
	}
	p := parseEdited(t, out)
	s := p.Find("ml-test-lab")
	if s == nil {
		t.Fatalf("share missing:\n%s", out)
	}
	if s.Limits.CPUPercent != 50 || s.Limits.Memory != "7Gi" || s.Limits.Disk != "20Gi" || s.Limits.GPU {
		t.Errorf("defaults wrong: %+v", s.Limits)
	}
	if !s.AcceptFrom.All || s.OnOwnerReturn != ReturnCheckpointAndStop || s.When.OnlyWhenIdle != "" {
		t.Errorf("share settings wrong: %+v", s)
	}
	if len(p.Priority) != 1 || p.Priority[0] != "ml-test-lab" {
		t.Errorf("priority = %v", p.Priority)
	}
	// The rest of the default policy is unchanged.
	if p.Egress != "deny" || p.Idle != "auto" || len(p.Isolation.AllowTiers) != 3 {
		t.Errorf("other settings changed: egress=%s idle=%s tiers=%v", p.Egress, p.Idle, p.Isolation.AllowTiers)
	}
	if got := s.Describe(); got != "up to 50% CPU, 7Gi memory, 20Gi disk, no GPU, no apps, at any time" {
		t.Errorf("Describe() = %q", got)
	}
}

func TestSecondAddUpdatesOnlyGivenFields(t *testing.T) {
	out, _, err := AddOrUpdateShare([]byte(DefaultYAML), ShareEdit{Group: "lab", OnlyWhenIdle: ptr("10m")},
		ShareDefaults{TotalMemoryMB: 8192})
	if err != nil {
		t.Fatal(err)
	}
	out, created, err := AddOrUpdateShare(out, ShareEdit{Group: "lab", CPUPercent: ptr(25), GPU: ptr(true)},
		ShareDefaults{TotalMemoryMB: 64 * 1024})
	if err != nil {
		t.Fatal(err)
	}
	if created {
		t.Fatal("second add must update, not create")
	}
	p := parseEdited(t, out)
	if len(p.Shares) != 1 || len(p.Priority) != 1 {
		t.Fatalf("duplicated: shares=%d priority=%v\n%s", len(p.Shares), p.Priority, out)
	}
	s := p.Shares[0]
	// Changed: cpu and gpu. Kept: memory from the first add (not the new default), disk, idle rule.
	if s.Limits.CPUPercent != 25 || !s.Limits.GPU || s.Limits.Memory != "4Gi" || s.Limits.Disk != "20Gi" {
		t.Errorf("limits = %+v", s.Limits)
	}
	if s.When.OnlyWhenIdle != "10m" {
		t.Errorf("idle rule lost: %+v", s.When)
	}
	if got := s.Describe(); !strings.Contains(got, "GPU allowed") || !strings.Contains(got, "only after 10m") {
		t.Errorf("Describe() = %q", got)
	}
}

func TestUpdateKeepsHandWrittenShare(t *testing.T) {
	raw := []byte(`shares:
  - group: research-team
    when:
      weekdays: "19:00-08:00"   # evenings
    limits: { cpu_percent: 75, memory: 24Gi, gpu: true, disk: 100Gi }
    accept_from: [alice, bob]
priority: [research-team]
idle: auto
`)
	out, created, err := AddOrUpdateShare(raw, ShareEdit{Group: "research-team", Memory: ptr("8Gi")}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	if created {
		t.Fatal("expected update")
	}
	if !strings.Contains(string(out), "# evenings") {
		t.Errorf("line comment lost:\n%s", out)
	}
	s := parseEdited(t, out).Find("research-team")
	if s.Limits.Memory != "8Gi" || s.Limits.CPUPercent != 75 || !s.Limits.GPU || s.AcceptFrom.All ||
		len(s.AcceptFrom.Handles) != 2 || !s.When.Weekdays.set {
		t.Errorf("share changed beyond memory: %+v", s)
	}
}

func TestAddSecondGroupAppendsToPriority(t *testing.T) {
	out, _, err := AddOrUpdateShare([]byte(DefaultYAML), ShareEdit{Group: "first"}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	out, _, err = AddOrUpdateShare(out, ShareEdit{Group: "second"}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	p := parseEdited(t, out)
	if len(p.Shares) != 2 || strings.Join(p.Priority, ",") != "first,second" {
		t.Errorf("shares=%d priority=%v", len(p.Shares), p.Priority)
	}
}

func TestRemoveShare(t *testing.T) {
	out, _, err := AddOrUpdateShare([]byte(DefaultYAML), ShareEdit{Group: "lab"}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	out, _, err = AddOrUpdateShare(out, ShareEdit{Group: "other"}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	out, removed, err := RemoveShare(out, "lab")
	if err != nil || !removed {
		t.Fatalf("removed=%v err=%v", removed, err)
	}
	p := parseEdited(t, out)
	if p.Find("lab") != nil || len(p.Shares) != 1 || strings.Join(p.Priority, ",") != "other" {
		t.Errorf("after remove: shares=%d priority=%v\n%s", len(p.Shares), p.Priority, out)
	}
	if !strings.Contains(string(out), "# The server can never widen this policy") {
		t.Errorf("comment lost:\n%s", out)
	}
	out, removed, err = RemoveShare(out, "other")
	if err != nil || !removed {
		t.Fatalf("removed=%v err=%v", removed, err)
	}
	if p := parseEdited(t, out); len(p.Shares) != 0 || len(p.Priority) != 0 {
		t.Errorf("not empty: %+v", p)
	}
	same, removed, err := RemoveShare(out, "never-shared")
	if err != nil || removed || string(same) != string(out) {
		t.Errorf("removing an unknown group must be a no-op: removed=%v err=%v", removed, err)
	}
}

func TestEditRejectsBadInput(t *testing.T) {
	cases := []ShareEdit{
		{Group: "Bad Name"},
		{Group: "../etc"},
		{Group: "lab", CPUPercent: ptr(0)},
		{Group: "lab", CPUPercent: ptr(101)},
		{Group: "lab", Memory: ptr("lots")},
		{Group: "lab", Disk: ptr("0Gi")},
		{Group: "lab", OnlyWhenIdle: ptr("soon")},
	}
	for _, c := range cases {
		if _, _, err := AddOrUpdateShare([]byte(DefaultYAML), c, ShareDefaults{}); err == nil {
			t.Errorf("expected an error for %+v", c)
		}
	}
	if _, _, err := RemoveShare([]byte(DefaultYAML), "Bad Name"); err == nil {
		t.Error("expected an error for a bad group name in RemoveShare")
	}
	if _, _, err := AddOrUpdateShare([]byte("- not\n- a mapping\n"), ShareEdit{Group: "lab"}, ShareDefaults{}); err == nil {
		t.Error("expected an error for a non-mapping policy")
	}
	// An edit that would leave the policy invalid (unknown key already there) is refused.
	if _, _, err := AddOrUpdateShare([]byte("shares: []\ntypo_key: 1\n"), ShareEdit{Group: "lab"}, ShareDefaults{}); err == nil {
		t.Error("expected the invalid result to be refused")
	}
}

func TestAddToEmptyFile(t *testing.T) {
	out, created, err := AddOrUpdateShare(nil, ShareEdit{Group: "lab"}, ShareDefaults{TotalMemoryMB: 1024})
	if err != nil || !created {
		t.Fatalf("created=%v err=%v", created, err)
	}
	s := parseEdited(t, out).Find("lab")
	if s == nil || s.Limits.Memory != "1Gi" {
		t.Fatalf("share = %+v\n%s", s, out)
	}
}

func TestDefaultShareMemory(t *testing.T) {
	for in, want := range map[int64]string{0: "4Gi", 1024: "1Gi", 3000: "1Gi", 15 * 1024: "7Gi", 64 * 1024: "32Gi"} {
		if got := DefaultShareMemory(in); got != want {
			t.Errorf("DefaultShareMemory(%d) = %s, want %s", in, got, want)
		}
	}
}

func TestShareAllowsApps(t *testing.T) {
	yes, hours := true, 2.5
	out, _, err := AddOrUpdateShare(nil, ShareEdit{Group: "lab", AllowApps: &yes}, ShareDefaults{TotalMemoryMB: 8192})
	if err != nil {
		t.Fatal(err)
	}
	p, err := Parse(out)
	if err != nil {
		t.Fatal(err)
	}
	if s := p.Find("lab"); !s.Apps.Allow || s.Apps.MaxHours != DefaultAppHours {
		t.Fatalf("apps: %+v", s.Apps)
	}
	out, _, err = AddOrUpdateShare(out, ShareEdit{Group: "lab", AppHours: &hours}, ShareDefaults{})
	if err != nil {
		t.Fatal(err)
	}
	p, _ = Parse(out)
	if s := p.Find("lab"); !s.Apps.Allow || s.Apps.MaxHours != 2.5 || !strings.Contains(s.Describe(), "apps allowed") {
		t.Fatalf("apps: %+v %s", s.Apps, s.Describe())
	}
	bad := 500.0
	if _, _, err := AddOrUpdateShare(out, ShareEdit{Group: "lab", AppHours: &bad}, ShareDefaults{}); err == nil {
		t.Fatal("500 hours accepted")
	}
}
