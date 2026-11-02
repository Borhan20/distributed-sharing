// Package idle reports how long the device owner has been away from the
// keyboard and mouse (plan §7.6).
package idle

import (
	"fmt"
	"time"
)

// Detector reports how long the owner has been idle. An error means
// "unknown"; callers must treat unknown as "owner active" (conservative).
type Detector interface {
	IdleFor() (time.Duration, error)
	Name() string
}

// Static always returns the same answer. Used in tests and when the owner
// configures `idle: assume-idle` (e.g. a headless lab machine) or
// `idle: assume-active`.
type Static struct {
	Idle bool
}

func (s Static) IdleFor() (time.Duration, error) {
	if s.Idle {
		return 365 * 24 * time.Hour, nil
	}
	return 0, nil
}

func (s Static) Name() string {
	if s.Idle {
		return "assume-idle"
	}
	return "assume-active"
}

// New returns the detector for a policy `idle:` mode: auto, assume-idle or
// assume-active.
func New(mode string) (Detector, error) {
	switch mode {
	case "", "auto":
		return platformDetector(), nil
	case "assume-idle":
		return Static{Idle: true}, nil
	case "assume-active":
		return Static{Idle: false}, nil
	}
	return nil, fmt.Errorf("unknown idle mode %q (want auto, assume-idle or assume-active)", mode)
}
