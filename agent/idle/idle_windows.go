//go:build windows

package idle

import (
	"errors"
	"time"
)

// TODO(plan §7.6): the agent's Windows service runs in session 0 and cannot
// see user input. A small helper in the user's session must report input
// activity (like HTCondor's condor_kbdd). Until it exists, the owner is
// reported as active, so shares with only_when_idle never run. UNTESTED.
type windowsStub struct{}

func platformDetector() Detector { return windowsStub{} }

func (windowsStub) Name() string { return "windows-stub" }

func (windowsStub) IdleFor() (time.Duration, error) {
	return 0, errors.New("idle detection on Windows needs the user-session helper (not built yet)")
}
