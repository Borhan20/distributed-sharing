//go:build !linux && !windows

package idle

import (
	"errors"
	"time"
)

type unsupported struct{}

func platformDetector() Detector { return unsupported{} }

func (unsupported) Name() string { return "unsupported" }

func (unsupported) IdleFor() (time.Duration, error) {
	return 0, errors.New("idle detection is not supported on this platform")
}
