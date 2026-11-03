// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

// Package policy parses and enforces the owner's local sharing policy
// (plan §6.4). The agent enforces it on every offer, whatever the server says.
package policy

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"pool/agent/protocol"
)

// Owner-return actions (plan §7.6).
const (
	ReturnCheckpointAndStop = "checkpoint-and-stop"
	ReturnPause             = "pause"
	ReturnThrottle          = "throttle"
)

// DefaultYAML is written at enrollment when no policy exists. It shares the
// device with no group: the owner must opt in by adding a share.
const DefaultYAML = `# pool agent local sharing policy (plan §6.4).
#
# This device is shared only with the groups listed under "shares" (none at
# first). To share it, run:  pool-agent share GROUP
# or add an entry by hand, for example:
#
# shares:
#   - group: research-team
#     when:
#       weekdays: "19:00-08:00"     # local time; ranges may cross midnight
#       weekends: all-day           # all-day | never | "HH:MM-HH:MM[,HH:MM-HH:MM]"
#       only_when_idle: 10m         # no keyboard or mouse input for 10 minutes
#     limits: { cpu_percent: 75, memory: 24Gi, gpu: true, disk: 100Gi }
#     accept_from: all-members      # or a list of user handles: [alice, bob]
#     accept_new_members: false     # accept jobs from members on probation
#     on_owner_return: checkpoint-and-stop   # or: pause | throttle
#
# The server can never widen this policy; the agent checks it on every task.
shares: []
priority: []
isolation:
  allow_tiers: [T1, T2, T3]   # T0 (bare metal) is off by default
egress: deny                  # jobs get no internet access
kill_switch: true             # "pool-agent kill" stops everything immediately
images:
  allow: []                   # optional image prefixes, e.g. registry.example.org/research/
idle: auto                    # auto | assume-idle | assume-active
always_on: false              # set true only for machines that never sleep
`

var groupRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,62}$`)
var digestRE = regexp.MustCompile(`^[^\s@]+@sha256:[0-9a-f]{64}$`)

// AcceptFrom mirrors protocol.AcceptFrom with YAML support.
type AcceptFrom struct{ protocol.AcceptFrom }

func (a *AcceptFrom) UnmarshalYAML(n *yaml.Node) error {
	switch n.Kind {
	case yaml.ScalarNode:
		if n.Value != protocol.AcceptAllMembers {
			return fmt.Errorf("line %d: accept_from must be %q or a list of handles", n.Line, protocol.AcceptAllMembers)
		}
		a.All = true
		return nil
	case yaml.SequenceNode:
		var list []string
		if err := n.Decode(&list); err != nil {
			return err
		}
		a.Handles = list
		return nil
	}
	return fmt.Errorf("line %d: accept_from must be %q or a list of handles", n.Line, protocol.AcceptAllMembers)
}

// Window is one day-type's schedule: all day, never, or a list of ranges.
type Window struct {
	set    bool
	all    bool
	ranges [][2]int // minutes since midnight; start > end means the range crosses midnight
	text   string
}

func (w *Window) UnmarshalYAML(n *yaml.Node) error {
	if n.Kind != yaml.ScalarNode {
		return fmt.Errorf("line %d: window must be all-day, never or \"HH:MM-HH:MM\"", n.Line)
	}
	parsed, err := ParseWindow(n.Value)
	if err != nil {
		return fmt.Errorf("line %d: %w", n.Line, err)
	}
	*w = parsed
	return nil
}

// ParseWindow parses "all-day", "never" or "HH:MM-HH:MM[,HH:MM-HH:MM...]".
func ParseWindow(s string) (Window, error) {
	s = strings.TrimSpace(s)
	switch s {
	case "all-day":
		return Window{set: true, all: true, text: s}, nil
	case "never":
		return Window{set: true, text: s}, nil
	}
	w := Window{set: true, text: s}
	for _, part := range strings.Split(s, ",") {
		a, b, ok := strings.Cut(strings.TrimSpace(part), "-")
		if !ok {
			return Window{}, fmt.Errorf("bad time range %q (want HH:MM-HH:MM)", part)
		}
		start, err := parseClock(a)
		if err != nil {
			return Window{}, err
		}
		end, err := parseClock(b)
		if err != nil {
			return Window{}, err
		}
		if start == end {
			return Window{}, fmt.Errorf("empty time range %q (use all-day)", part)
		}
		w.ranges = append(w.ranges, [2]int{start, end})
	}
	return w, nil
}

func parseClock(s string) (int, error) {
	h, m, ok := strings.Cut(strings.TrimSpace(s), ":")
	if !ok {
		return 0, fmt.Errorf("bad time %q (want HH:MM)", s)
	}
	hh, err1 := strconv.Atoi(h)
	mm, err2 := strconv.Atoi(m)
	if err1 != nil || err2 != nil || hh < 0 || hh > 24 || mm < 0 || mm > 59 || (hh == 24 && mm != 0) || len(m) != 2 {
		return 0, fmt.Errorf("bad time %q (want HH:MM)", s)
	}
	return hh*60 + mm, nil
}

// contains reports whether minute-of-day m falls in the window. A range that
// crosses midnight is evaluated against the current day's type: on a weekday,
// "19:00-08:00" covers 00:00-08:00 and 19:00-24:00.
func (w Window) contains(m int) bool {
	if w.all {
		return true
	}
	for _, r := range w.ranges {
		if r[0] < r[1] {
			if m >= r[0] && m < r[1] {
				return true
			}
		} else if m >= r[0] || m < r[1] {
			return true
		}
	}
	return false
}

// When is a share's schedule.
type When struct {
	Weekdays     Window `yaml:"weekdays"`
	Weekends     Window `yaml:"weekends"`
	OnlyWhenIdle string `yaml:"only_when_idle"`

	idle time.Duration
}

// ActiveAt reports whether the time window covers t (local time of t).
// With neither weekdays nor weekends set, every day is all-day. If only one
// is set, the other day type is never active.
func (w When) ActiveAt(t time.Time) bool {
	if !w.Weekdays.set && !w.Weekends.set {
		return true
	}
	m := t.Hour()*60 + t.Minute()
	switch t.Weekday() {
	case time.Saturday, time.Sunday:
		return w.Weekends.set && w.Weekends.contains(m)
	default:
		return w.Weekdays.set && w.Weekdays.contains(m)
	}
}

// IdleRequired returns the required idle duration (0 = not required).
func (w When) IdleRequired() time.Duration { return w.idle }

// Limits caps what one group may use on this device.
type Limits struct {
	CPUPercent int    `yaml:"cpu_percent"`
	Memory     string `yaml:"memory"`
	GPU        bool   `yaml:"gpu"`
	Disk       string `yaml:"disk"`

	memoryMB int64
	diskMB   int64
}

func (l Limits) MemoryMB() int64 { return l.memoryMB }
func (l Limits) DiskMB() int64   { return l.diskMB }

// Share is one group the device is shared with.
type Share struct {
	Group            string     `yaml:"group"`
	When             When       `yaml:"when"`
	Limits           Limits     `yaml:"limits"`
	AcceptFrom       AcceptFrom `yaml:"accept_from"`
	AcceptNewMembers bool       `yaml:"accept_new_members"`
	OnOwnerReturn    string     `yaml:"on_owner_return"`
}

// Isolation lists the tiers the owner allows.
type Isolation struct {
	AllowTiers []protocol.Tier `yaml:"allow_tiers"`
}

// Images is an optional image prefix allowlist (threat model #8).
type Images struct {
	Allow []string `yaml:"allow"`
}

// Policy is the parsed local policy file.
type Policy struct {
	Shares     []Share   `yaml:"shares"`
	Priority   []string  `yaml:"priority"`
	Isolation  Isolation `yaml:"isolation"`
	Egress     string    `yaml:"egress"`
	KillSwitch *bool     `yaml:"kill_switch"`
	Images     Images    `yaml:"images"`
	Idle       string    `yaml:"idle"`
	AlwaysOn   bool      `yaml:"always_on"`

	Digest string `yaml:"-"`
	Path   string `yaml:"-"`
}

// Load reads and validates a policy file.
func Load(path string) (*Policy, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	p, err := Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("policy %s: %w", path, err)
	}
	p.Path = path
	return p, nil
}

// Parse validates policy YAML. Unknown keys are errors, so a typo cannot
// silently widen or narrow what the owner meant.
func Parse(raw []byte) (*Policy, error) {
	p := &Policy{}
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(p); err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	p.Digest = "sha256:" + hex.EncodeToString(sum[:])
	return p, p.validate()
}

func (p *Policy) validate() error {
	seen := map[string]bool{}
	for i := range p.Shares {
		s := &p.Shares[i]
		if !groupRE.MatchString(s.Group) {
			return fmt.Errorf("shares[%d]: invalid group name %q", i, s.Group)
		}
		if seen[s.Group] {
			return fmt.Errorf("shares[%d]: group %q listed twice", i, s.Group)
		}
		seen[s.Group] = true
		if s.Limits.CPUPercent < 1 || s.Limits.CPUPercent > 100 {
			return fmt.Errorf("shares[%d] (%s): limits.cpu_percent must be 1-100", i, s.Group)
		}
		var err error
		if s.Limits.memoryMB, err = ParseQuantityMB(s.Limits.Memory); err != nil || s.Limits.memoryMB <= 0 {
			return fmt.Errorf("shares[%d] (%s): limits.memory: %v", i, s.Group, orMissing(err))
		}
		if s.Limits.diskMB, err = ParseQuantityMB(s.Limits.Disk); err != nil || s.Limits.diskMB <= 0 {
			return fmt.Errorf("shares[%d] (%s): limits.disk: %v", i, s.Group, orMissing(err))
		}
		if !s.AcceptFrom.All && s.AcceptFrom.Handles == nil {
			return fmt.Errorf("shares[%d] (%s): accept_from is required (all-members or a list of handles)", i, s.Group)
		}
		if s.When.OnlyWhenIdle != "" {
			d, err := time.ParseDuration(s.When.OnlyWhenIdle)
			if err != nil || d < 0 {
				return fmt.Errorf("shares[%d] (%s): when.only_when_idle: bad duration %q", i, s.Group, s.When.OnlyWhenIdle)
			}
			s.When.idle = d
		}
		switch s.OnOwnerReturn {
		case "":
			s.OnOwnerReturn = ReturnCheckpointAndStop
		case ReturnCheckpointAndStop, ReturnPause, ReturnThrottle:
		default:
			return fmt.Errorf("shares[%d] (%s): on_owner_return must be checkpoint-and-stop, pause or throttle", i, s.Group)
		}
	}
	for _, g := range p.Priority {
		if !seen[g] {
			return fmt.Errorf("priority lists %q, which is not in shares", g)
		}
	}
	if p.Isolation.AllowTiers == nil {
		p.Isolation.AllowTiers = []protocol.Tier{protocol.T1, protocol.T2, protocol.T3}
	}
	for _, t := range p.Isolation.AllowTiers {
		if t.Rank() < 0 {
			return fmt.Errorf("isolation.allow_tiers: unknown tier %q", t)
		}
	}
	switch p.Egress {
	case "":
		p.Egress = "deny"
	case "deny", "allow":
	default:
		return fmt.Errorf("egress must be deny or allow")
	}
	switch p.Idle {
	case "":
		p.Idle = "auto"
	case "auto", "assume-idle", "assume-active":
	default:
		return fmt.Errorf("idle must be auto, assume-idle or assume-active")
	}
	for _, prefix := range p.Images.Allow {
		if strings.TrimSpace(prefix) == "" {
			return errors.New("images.allow: empty prefix")
		}
	}
	return nil
}

func orMissing(err error) error {
	if err == nil {
		return errors.New("must be greater than zero")
	}
	return err
}

// WriteDefault writes DefaultYAML to path unless a file already exists.
func WriteDefault(path string) (bool, error) {
	if _, err := os.Stat(path); err == nil {
		return false, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return false, err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return false, err
	}
	defer f.Close()
	_, err = f.WriteString(DefaultYAML)
	return err == nil, err
}

var quantityRE = regexp.MustCompile(`^\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?\s*$`)

// ParseQuantityMB parses "24Gi" -> 24576 MiB. Plain numbers are MiB.
func ParseQuantityMB(s string) (int64, error) {
	m := quantityRE.FindStringSubmatch(s)
	if m == nil {
		return 0, fmt.Errorf("bad quantity %q (use e.g. 512Mi, 16Gi)", s)
	}
	n, _ := strconv.ParseFloat(m[1], 64)
	const mib = 1024 * 1024
	factor := map[string]float64{
		"": 1, "Ki": 1.0 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024,
		"K": 1e3 / mib, "M": 1e6 / mib, "G": 1e9 / mib, "T": 1e12 / mib,
	}[m[2]]
	return int64(n*factor + 0.5), nil
}

// Find returns the share for a group, or nil.
func (p *Policy) Find(group string) *Share {
	for i := range p.Shares {
		if p.Shares[i].Group == group {
			return &p.Shares[i]
		}
	}
	return nil
}

// Ordered returns shares in the owner's priority order: groups listed in
// `priority` first, then the rest in file order.
func (p *Policy) Ordered() []*Share {
	out := make([]*Share, 0, len(p.Shares))
	used := map[string]bool{}
	for _, g := range p.Priority {
		if s := p.Find(g); s != nil {
			out = append(out, s)
			used[g] = true
		}
	}
	for i := range p.Shares {
		if !used[p.Shares[i].Group] {
			out = append(out, &p.Shares[i])
		}
	}
	return out
}

// TierAllowed reports whether the owner allows a tier.
func (p *Policy) TierAllowed(t protocol.Tier) bool {
	for _, a := range p.Isolation.AllowTiers {
		if a == t {
			return true
		}
	}
	return false
}

// AllowedTiers intersects available tiers with the owner's allow list.
func (p *Policy) AllowedTiers(available []protocol.Tier) []protocol.Tier {
	out := []protocol.Tier{}
	for _, t := range available {
		if p.TierAllowed(t) {
			out = append(out, t)
		}
	}
	return out
}

// IdleState is the owner-activity input for a decision.
type IdleState struct {
	IdleFor time.Duration
	Err     error // unknown idle state counts as "owner active"
}

// Active reports whether a share may run work right now.
func (s *Share) Active(now time.Time, idle IdleState) (bool, string) {
	if !s.When.ActiveAt(now) {
		return false, protocol.DeclineOutsideWindow
	}
	if need := s.When.IdleRequired(); need > 0 {
		if idle.Err != nil || idle.IdleFor < need {
			return false, protocol.DeclineOwnerActive
		}
	}
	return true, ""
}

// ShareInfos returns the ShareInfo list reported in hello and heartbeat.
func (p *Policy) ShareInfos(device Device, now time.Time, idle IdleState) []protocol.ShareInfo {
	out := []protocol.ShareInfo{}
	for _, s := range p.Ordered() {
		active, _ := s.Active(now, idle)
		out = append(out, protocol.ShareInfo{
			Group: s.Group,
			Limits: protocol.ShareLimits{
				CPUPercent: s.Limits.CPUPercent, MemoryMB: s.Limits.memoryMB, DiskMB: s.Limits.diskMB,
				GPU: s.Limits.GPU && len(device.GPUs) > 0,
			},
			AcceptFrom:       s.AcceptFrom.AcceptFrom,
			AcceptNewMembers: s.AcceptNewMembers,
			ActiveNow:        active,
		})
	}
	return out
}

// Device describes what this machine has.
type Device struct {
	CPUCores   int
	MemoryMB   int64
	FreeDiskMB int64
	GPUs       []protocol.GPUInfo
	Tiers      []protocol.Tier // tiers the sandbox can provide
	GPUTiers   []protocol.Tier // tiers that can also expose a GPU
}

// Used is what running attempts consume.
type Used struct {
	CPU      float64
	MemoryMB int64
	DiskMB   int64
	GPUs     int
}

func (u Used) Add(r protocol.TaskResources) Used {
	return Used{CPU: u.CPU + r.CPU, MemoryMB: u.MemoryMB + r.MemoryMB, DiskMB: u.DiskMB + r.DiskMB, GPUs: u.GPUs + r.GPUs}
}

// Decision is the result of an admission check.
type Decision struct {
	OK     bool
	Reason string
	Detail string
	Tier   protocol.Tier
	Share  *Share
}

func deny(reason, detail string) Decision { return Decision{Reason: reason, Detail: detail} }

// Admit decides whether to run a verified envelope (plan §7.1 "device
// admission"). byGroup is per-group usage, total is device-wide usage.
func (p *Policy) Admit(env *protocol.TaskEnvelope, dev Device, byGroup map[string]Used, total Used,
	now time.Time, idle IdleState) Decision {
	share := p.Find(env.Group)
	if share == nil {
		return deny(protocol.DeclineGroupNotShared, fmt.Sprintf("group %q is not in the local policy", env.Group))
	}
	spec := env.Spec
	if !digestRE.MatchString(spec.Image) {
		return deny(protocol.DeclineImageNotAllowed, "image is not pinned by digest")
	}
	if len(p.Images.Allow) > 0 {
		ok := false
		for _, prefix := range p.Images.Allow {
			if strings.HasPrefix(spec.Image, prefix) {
				ok = true
				break
			}
		}
		if !ok {
			return deny(protocol.DeclineImageNotAllowed, "image is not in images.allow")
		}
	}
	if spec.Network.Egress != "" && spec.Network.Egress != "none" {
		// Design doc X5: no egress proxy yet, so egress is always declined.
		return deny(protocol.DeclineEgressNotAllowed, "network egress is not supported by this agent yet")
	}
	if active, reason := share.Active(now, idle); !active {
		return deny(reason, "share is not active now")
	}
	if !share.AcceptFrom.Accepts(env.Submitter) {
		return deny(protocol.DeclineSubmitterNotAccepted, fmt.Sprintf("submitter %q is not in accept_from", env.Submitter))
	}
	if env.SubmitterProbation && !share.AcceptNewMembers {
		return deny(protocol.DeclineProbationNotAccepted, "submitter is on probation")
	}
	tier, ok := p.ChooseTier(spec, dev)
	if !ok {
		return deny(protocol.DeclineTierNotAllowed, fmt.Sprintf("no allowed tier >= %s", spec.MinTier))
	}
	if detail := checkResources(spec.Resources, share, dev, byGroup[env.Group], total); detail != "" {
		return deny(protocol.DeclineResourcesExceedLimits, detail)
	}
	return Decision{OK: true, Tier: tier, Share: share}
}

// ChooseTier picks the strongest allowed tier that meets min_tier. GPU jobs
// can only use tiers that expose a GPU.
func (p *Policy) ChooseTier(spec protocol.TaskSpec, dev Device) (protocol.Tier, bool) {
	min := spec.MinTier.Rank()
	if min < 0 {
		return "", false
	}
	candidates := dev.Tiers
	if spec.Resources.GPUs > 0 {
		candidates = dev.GPUTiers
	}
	best := protocol.Tier("")
	for _, t := range candidates {
		if !p.TierAllowed(t) || t.Rank() < min {
			continue
		}
		if best == "" || t.Rank() > best.Rank() {
			best = t
		}
	}
	return best, best != ""
}

func checkResources(r protocol.TaskResources, s *Share, dev Device, group, total Used) string {
	cpuLimit := float64(dev.CPUCores) * float64(s.Limits.CPUPercent) / 100
	switch {
	case group.CPU+r.CPU > cpuLimit+1e-9:
		return fmt.Sprintf("cpu %.2f + %.2f in use > share limit %.2f", r.CPU, group.CPU, cpuLimit)
	case total.CPU+r.CPU > float64(dev.CPUCores)+1e-9:
		return "cpu exceeds the device"
	case group.MemoryMB+r.MemoryMB > s.Limits.memoryMB:
		return fmt.Sprintf("memory %d MiB + %d in use > share limit %d", r.MemoryMB, group.MemoryMB, s.Limits.memoryMB)
	case dev.MemoryMB > 0 && total.MemoryMB+r.MemoryMB > dev.MemoryMB:
		return "memory exceeds the device"
	case group.DiskMB+r.DiskMB > s.Limits.diskMB:
		return fmt.Sprintf("disk %d MiB + %d in use > share limit %d", r.DiskMB, group.DiskMB, s.Limits.diskMB)
	case dev.FreeDiskMB > 0 && r.DiskMB > dev.FreeDiskMB:
		return "not enough free disk"
	}
	if r.GPUs > 0 {
		if !s.Limits.GPU {
			return "share does not allow GPU use"
		}
		if total.GPUs+r.GPUs > len(dev.GPUs) {
			return fmt.Sprintf("needs %d GPU(s), %d free", r.GPUs, len(dev.GPUs)-total.GPUs)
		}
		if r.GPUVendor != nil && *r.GPUVendor != "" {
			for _, g := range dev.GPUs {
				if !strings.EqualFold(g.Vendor, *r.GPUVendor) {
					return "GPU vendor does not match"
				}
			}
		}
		for _, g := range dev.GPUs {
			if r.GPUMemoryMB > g.MemoryMB {
				return "GPU memory too small"
			}
		}
	}
	return ""
}

// Free returns the capacity advertised in `ready`: the largest amount any
// active share could still accept, capped by what the device has free.
func (p *Policy) Free(dev Device, byGroup map[string]Used, total Used, now time.Time, idle IdleState) (protocol.Resources, bool) {
	var best protocol.Resources
	any := false
	devCPU := float64(dev.CPUCores) - total.CPU
	devMem := dev.MemoryMB - total.MemoryMB
	devGPU := len(dev.GPUs) - total.GPUs
	for _, s := range p.Ordered() {
		if ok, _ := s.Active(now, idle); !ok {
			continue
		}
		u := byGroup[s.Group]
		cpu := minF(float64(dev.CPUCores)*float64(s.Limits.CPUPercent)/100-u.CPU, devCPU)
		mem := minI(s.Limits.memoryMB-u.MemoryMB, devMem)
		disk := s.Limits.diskMB - u.DiskMB
		if dev.FreeDiskMB > 0 {
			disk = minI(disk, dev.FreeDiskMB)
		}
		gpus := 0
		if s.Limits.GPU {
			gpus = devGPU
		}
		if cpu <= 0 || mem <= 0 {
			continue
		}
		any = true
		best.CPU = maxF(best.CPU, cpu)
		best.MemoryMB = maxI(best.MemoryMB, mem)
		best.DiskMB = maxI(best.DiskMB, disk)
		if gpus > best.GPUs {
			best.GPUs = gpus
		}
	}
	return best, any
}

func minF(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
func maxF(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
func minI(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
func maxI(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
