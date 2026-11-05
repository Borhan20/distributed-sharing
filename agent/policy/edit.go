// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package policy

import (
	"bytes"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// ShareEdit describes a change to one group's share. Nil fields keep the
// current value, or take the default when the share is new.
type ShareEdit struct {
	Group        string
	CPUPercent   *int
	Memory       *string
	Disk         *string
	GPU          *bool
	OnlyWhenIdle *string
	AllowApps    *bool    // apps (tasks with ports)
	AppHours     *float64 // the longest one app may run
}

// ShareDefaults supplies device-specific defaults for a new share.
type ShareDefaults struct {
	TotalMemoryMB int64 // the device's RAM; 0 if unknown
}

// Defaults for a new share: half the CPU, half the RAM, no GPU, at any time.
const (
	DefaultSharePercent = 50
	DefaultShareDisk    = "20Gi"
)

// DefaultShareMemory is half of the device's RAM, rounded down to whole GiB,
// at least 1Gi. Unknown RAM gives 4Gi.
func DefaultShareMemory(totalMB int64) string {
	if totalMB <= 0 {
		return "4Gi"
	}
	gi := totalMB / 2 / 1024
	if gi < 1 {
		gi = 1
	}
	return fmt.Sprintf("%dGi", gi)
}

// AddOrUpdateShare edits policy YAML so that it shares the device with
// e.Group. An existing share changes only the fields set in e; a new share
// gets defaults for the rest and is appended to the priority list. Comments
// in the file are kept. The result is validated with Parse.
func AddOrUpdateShare(raw []byte, e ShareEdit, d ShareDefaults) ([]byte, bool, error) {
	if err := checkEdit(e); err != nil {
		return nil, false, err
	}
	doc, root, err := loadDoc(raw)
	if err != nil {
		return nil, false, err
	}
	shares := mapValue(root, "shares", yaml.SequenceNode)
	shares.Style &^= yaml.FlowStyle // a list of mappings reads better in block style
	item := findShare(shares, e.Group)
	created := item == nil
	if created {
		item = &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
		setScalar(item, "group", e.Group, "!!str")
		if e.OnlyWhenIdle != nil {
			setScalar(mapValue(item, "when", yaml.MappingNode), "only_when_idle", *e.OnlyWhenIdle, "!!str")
		}
		limits := mapValue(item, "limits", yaml.MappingNode)
		setScalar(limits, "cpu_percent", strconv.Itoa(intOr(e.CPUPercent, DefaultSharePercent)), "!!int")
		setScalar(limits, "memory", strOr(e.Memory, DefaultShareMemory(d.TotalMemoryMB)), "!!str")
		setScalar(limits, "gpu", strconv.FormatBool(e.GPU != nil && *e.GPU), "!!bool")
		setScalar(limits, "disk", strOr(e.Disk, DefaultShareDisk), "!!str")
		setScalar(item, "accept_from", "all-members", "!!str")
		setScalar(item, "on_owner_return", ReturnCheckpointAndStop, "!!str")
		shares.Content = append(shares.Content, item)
	} else {
		if e.OnlyWhenIdle != nil {
			setScalar(mapValue(item, "when", yaml.MappingNode), "only_when_idle", *e.OnlyWhenIdle, "!!str")
		}
		if e.CPUPercent != nil || e.Memory != nil || e.Disk != nil || e.GPU != nil {
			limits := mapValue(item, "limits", yaml.MappingNode)
			if e.CPUPercent != nil {
				setScalar(limits, "cpu_percent", strconv.Itoa(*e.CPUPercent), "!!int")
			}
			if e.Memory != nil {
				setScalar(limits, "memory", *e.Memory, "!!str")
			}
			if e.GPU != nil {
				setScalar(limits, "gpu", strconv.FormatBool(*e.GPU), "!!bool")
			}
			if e.Disk != nil {
				setScalar(limits, "disk", *e.Disk, "!!str")
			}
		}
	}
	if e.AllowApps != nil || e.AppHours != nil {
		apps := mapValue(item, "apps", yaml.MappingNode)
		if e.AllowApps != nil {
			setScalar(apps, "allow", strconv.FormatBool(*e.AllowApps), "!!bool")
		}
		if e.AppHours != nil {
			setScalar(apps, "max_hours", strconv.FormatFloat(*e.AppHours, 'f', -1, 64), "!!float")
		}
	}
	prio := mapValue(root, "priority", yaml.SequenceNode)
	if indexOfScalar(prio, e.Group) < 0 {
		prio.Content = append(prio.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: e.Group})
	}
	out, err := encodeDoc(doc)
	if err != nil {
		return nil, false, err
	}
	return out, created, nil
}

// RemoveShare removes the group's share and its priority entry. removed is
// false (and the input is returned unchanged) when the group was not shared.
func RemoveShare(raw []byte, group string) ([]byte, bool, error) {
	if !groupRE.MatchString(group) {
		return nil, false, fmt.Errorf("invalid group name %q", group)
	}
	doc, root, err := loadDoc(raw)
	if err != nil {
		return nil, false, err
	}
	removed := false
	if shares := lookup(root, "shares"); shares != nil && shares.Kind == yaml.SequenceNode {
		kept := shares.Content[:0]
		for _, it := range shares.Content {
			if g := lookup(it, "group"); g != nil && g.Value == group {
				removed = true
				continue
			}
			kept = append(kept, it)
		}
		shares.Content = kept
	}
	if prio := lookup(root, "priority"); prio != nil && prio.Kind == yaml.SequenceNode {
		if i := indexOfScalar(prio, group); i >= 0 {
			prio.Content = append(prio.Content[:i], prio.Content[i+1:]...)
			removed = true
		}
	}
	if !removed {
		return raw, false, nil
	}
	out, err := encodeDoc(doc)
	if err != nil {
		return nil, false, err
	}
	return out, true, nil
}

// Describe summarises a share in plain words, e.g. "up to 50% CPU, 7Gi
// memory, 20Gi disk, no GPU, no apps, at any time".
func (s *Share) Describe() string {
	gpu := "no GPU"
	if s.Limits.GPU {
		gpu = "GPU allowed"
	}
	when := "at any time"
	switch {
	case s.When.Weekdays.set || s.When.Weekends.set:
		when = "during the hours set in the policy file"
	case s.When.OnlyWhenIdle != "":
		when = "only after " + s.When.OnlyWhenIdle + " without keyboard or mouse use"
	}
	if (s.When.Weekdays.set || s.When.Weekends.set) && s.When.OnlyWhenIdle != "" {
		when += ", and only after " + s.When.OnlyWhenIdle + " without keyboard or mouse use"
	}
	apps := "no apps"
	if s.Apps.Allow {
		apps = fmt.Sprintf("apps allowed (up to %g h each, with no network)", s.Apps.MaxHours)
	}
	return fmt.Sprintf("up to %d%% CPU, %s memory, %s disk, %s, %s, %s",
		s.Limits.CPUPercent, s.Limits.Memory, s.Limits.Disk, gpu, apps, when)
}

func checkEdit(e ShareEdit) error {
	if !groupRE.MatchString(e.Group) {
		return fmt.Errorf("invalid group name %q (use the group's short name, e.g. ml-test-lab)", e.Group)
	}
	if e.CPUPercent != nil && (*e.CPUPercent < 1 || *e.CPUPercent > 100) {
		return errors.New("--cpu-percent must be between 1 and 100")
	}
	for name, q := range map[string]*string{"--memory": e.Memory, "--disk": e.Disk} {
		if q == nil {
			continue
		}
		if mb, err := ParseQuantityMB(*q); err != nil || mb <= 0 {
			return fmt.Errorf("%s: bad size %q (use e.g. 512Mi, 8Gi)", name, *q)
		}
	}
	if e.AppHours != nil && (*e.AppHours <= 0 || *e.AppHours > 168) {
		return errors.New("--app-hours must be more than 0 and at most 168 (a week)")
	}
	if e.OnlyWhenIdle != nil {
		if d, err := time.ParseDuration(*e.OnlyWhenIdle); err != nil || d < 0 {
			return fmt.Errorf("--only-when-idle: bad duration %q (use e.g. 10m)", *e.OnlyWhenIdle)
		}
	}
	return nil
}

// loadDoc parses raw YAML into a document node whose root is a mapping.
// Empty input yields an empty mapping.
func loadDoc(raw []byte) (*yaml.Node, *yaml.Node, error) {
	doc := &yaml.Node{}
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := yaml.Unmarshal(raw, doc); err != nil {
			return nil, nil, fmt.Errorf("policy file is not valid YAML: %w", err)
		}
	}
	if doc.Kind == 0 {
		doc = &yaml.Node{Kind: yaml.DocumentNode, Content: []*yaml.Node{{Kind: yaml.MappingNode, Tag: "!!map"}}}
	}
	if doc.Kind != yaml.DocumentNode || len(doc.Content) != 1 || doc.Content[0].Kind != yaml.MappingNode {
		return nil, nil, errors.New("policy file must be a YAML mapping")
	}
	return doc, doc.Content[0], nil
}

func encodeDoc(doc *yaml.Node) ([]byte, error) {
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2)
	if err := enc.Encode(doc); err != nil {
		return nil, err
	}
	if err := enc.Close(); err != nil {
		return nil, err
	}
	out := buf.Bytes()
	if _, err := Parse(out); err != nil {
		return nil, fmt.Errorf("the edited policy would be invalid: %w", err)
	}
	return out, nil
}

// lookup returns the value node for key in a mapping node, or nil.
func lookup(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// mapValue returns the value for key, creating it with the given kind when
// missing or null.
func mapValue(m *yaml.Node, key string, kind yaml.Kind) *yaml.Node {
	if v := lookup(m, key); v != nil {
		if v.Kind == kind {
			return v
		}
		if v.Kind == yaml.ScalarNode && (v.Tag == "!!null" || v.Value == "" || v.Value == "~") {
			v.Kind, v.Tag, v.Value, v.Style = kind, tagFor(kind), "", 0
			return v
		}
		return v // wrong kind: Parse reports it
	}
	v := &yaml.Node{Kind: kind, Tag: tagFor(kind)}
	m.Content = append(m.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}, v)
	return v
}

func tagFor(k yaml.Kind) string {
	if k == yaml.SequenceNode {
		return "!!seq"
	}
	return "!!map"
}

// setScalar sets key to a scalar value, keeping the key's comments.
func setScalar(m *yaml.Node, key, value, tag string) {
	if v := lookup(m, key); v != nil {
		v.Kind, v.Tag, v.Value, v.Style, v.Content = yaml.ScalarNode, tag, value, 0, nil
		return
	}
	m.Content = append(m.Content,
		&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key},
		&yaml.Node{Kind: yaml.ScalarNode, Tag: tag, Value: value})
}

func findShare(shares *yaml.Node, group string) *yaml.Node {
	for _, it := range shares.Content {
		if g := lookup(it, "group"); g != nil && strings.TrimSpace(g.Value) == group {
			return it
		}
	}
	return nil
}

func indexOfScalar(seq *yaml.Node, value string) int {
	for i, n := range seq.Content {
		if n.Kind == yaml.ScalarNode && n.Value == value {
			return i
		}
	}
	return -1
}

func intOr(p *int, def int) int {
	if p != nil {
		return *p
	}
	return def
}

func strOr(p *string, def string) string {
	if p != nil {
		return *p
	}
	return def
}
