//go:build linux

package idle

// Linux idle detection through systemd-logind. TO BE VALIDATED IN SPIKE S4:
// IdleHint is only set by some desktop environments, and headless machines
// have no graphical session at all.

import (
	"errors"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type logind struct {
	now func() time.Time
	run func(args ...string) (string, error)
}

func platformDetector() Detector {
	return &logind{
		now: time.Now,
		run: func(args ...string) (string, error) {
			out, err := exec.Command("loginctl", args...).Output()
			return string(out), err
		},
	}
}

func (l *logind) Name() string { return "logind" }

// IdleFor returns the shortest idle time across local graphical and tty
// sessions. If any session is not idle, the owner is active (0). With no
// sessions at all the device is treated as idle since boot.
func (l *logind) IdleFor() (time.Duration, error) {
	list, err := l.run("list-sessions", "--no-legend", "--no-pager")
	if err != nil {
		return 0, err
	}
	var ids []string
	for _, line := range strings.Split(list, "\n") {
		f := strings.Fields(line)
		if len(f) > 0 {
			ids = append(ids, f[0])
		}
	}
	if len(ids) == 0 {
		return 365 * 24 * time.Hour, nil
	}
	shortest := time.Duration(-1)
	seen := 0
	for _, id := range ids {
		out, err := l.run("show-session", id, "-p", "IdleHint", "-p", "IdleSinceHint", "-p", "Remote", "-p", "Class")
		if err != nil {
			continue
		}
		props := parseProps(out)
		if props["Remote"] == "yes" || (props["Class"] != "" && props["Class"] != "user") {
			continue
		}
		seen++
		if props["IdleHint"] != "yes" {
			return 0, nil
		}
		since, err := strconv.ParseInt(props["IdleSinceHint"], 10, 64)
		if err != nil || since <= 0 {
			return 0, nil
		}
		d := l.now().Sub(time.UnixMicro(since))
		if shortest < 0 || d < shortest {
			shortest = d
		}
	}
	if seen == 0 {
		return 365 * 24 * time.Hour, nil
	}
	if shortest < 0 {
		return 0, errors.New("no idle information")
	}
	return shortest, nil
}

func parseProps(out string) map[string]string {
	m := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), "="); ok {
			m[k] = v
		}
	}
	return m
}
