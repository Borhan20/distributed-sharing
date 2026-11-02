//go:build linux

package idle

import (
	"errors"
	"strconv"
	"testing"
	"time"
)

func fake(now time.Time, sessions map[string]string, listErr error) *logind {
	return &logind{
		now: func() time.Time { return now },
		run: func(args ...string) (string, error) {
			if args[0] == "list-sessions" {
				if listErr != nil {
					return "", listErr
				}
				out := ""
				for id := range sessions {
					out += id + " 1000 owner seat0 tty2\n"
				}
				return out, nil
			}
			return sessions[args[1]], nil
		},
	}
}

func TestLogind(t *testing.T) {
	now := time.Date(2026, 11, 2, 12, 0, 0, 0, time.UTC)
	since := strconv.FormatInt(now.Add(-20*time.Minute).UnixMicro(), 10)
	idle := "IdleHint=yes\nIdleSinceHint=" + since + "\nRemote=no\nClass=user\n"
	busy := "IdleHint=no\nIdleSinceHint=0\nRemote=no\nClass=user\n"
	remote := "IdleHint=no\nIdleSinceHint=0\nRemote=yes\nClass=user\n"

	d, err := fake(now, map[string]string{"1": idle}, nil).IdleFor()
	if err != nil || d != 20*time.Minute {
		t.Fatalf("idle session: %v %v", d, err)
	}
	d, _ = fake(now, map[string]string{"1": idle, "2": busy}, nil).IdleFor()
	if d != 0 {
		t.Fatalf("one active session must mean active, got %v", d)
	}
	d, _ = fake(now, map[string]string{"3": remote}, nil).IdleFor()
	if d < 24*time.Hour {
		t.Fatalf("remote-only sessions should count as idle, got %v", d)
	}
	d, _ = fake(now, map[string]string{}, nil).IdleFor()
	if d < 24*time.Hour {
		t.Fatalf("no sessions should count as idle, got %v", d)
	}
	if _, err := fake(now, nil, errors.New("no loginctl")).IdleFor(); err == nil {
		t.Fatal("expected error when loginctl fails")
	}
}

func TestNew(t *testing.T) {
	for _, m := range []string{"", "auto", "assume-idle", "assume-active"} {
		if _, err := New(m); err != nil {
			t.Errorf("New(%q): %v", m, err)
		}
	}
	if _, err := New("sometimes"); err == nil {
		t.Error("bad mode accepted")
	}
	d, _ := Static{Idle: true}.IdleFor()
	if d < time.Hour {
		t.Error("assume-idle should be idle")
	}
}
