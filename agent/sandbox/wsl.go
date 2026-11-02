// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package sandbox

// Windows runtime: jobs run in a dedicated WSL2 distro with a rootless
// Docker Engine inside it (plan §8.5).
//
// UNTESTED: there is no Windows host in this environment. Spike S2 must
// validate this code path (rootless engine, NVIDIA CDI, escape attempts to
// Windows files) before it is used.

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path"
	"strings"
	"time"
)

// WSLDirs creates output directories inside the WSL2 distro, because
// automount is disabled and the daemon cannot see Windows paths.
type WSLDirs struct {
	Distro string
	User   string
	Root   string // e.g. /var/lib/pool-agent/work (inside the distro)
}

func (w WSLDirs) run(args ...string) error {
	full := append([]string{"-d", w.Distro, "-u", w.User, "--"}, args...)
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "wsl.exe", full...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("wsl %s: %w: %s", args[0], err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (w WSLDirs) Prepare(attemptID string) (string, error) {
	if strings.ContainsAny(attemptID, "/\\. ") {
		return "", fmt.Errorf("bad attempt id %q", attemptID)
	}
	dir := path.Join(w.Root, attemptID)
	out := path.Join(dir, "out")
	if err := w.run("mkdir", "-p", "-m", "0700", dir); err != nil {
		return "", err
	}
	if err := w.run("mkdir", "-p", out); err != nil {
		return "", err
	}
	return out, w.run("chmod", "0777", out)
}

func (w WSLDirs) Remove(attemptID string) error {
	if strings.ContainsAny(attemptID, "/\\. ") || attemptID == "" {
		return fmt.Errorf("bad attempt id %q", attemptID)
	}
	return w.run("rm", "-rf", "--", path.Join(w.Root, attemptID))
}

// NewWSLDocker returns the Docker runtime that drives the engine inside a
// dedicated WSL2 distro. UNTESTED.
func NewWSLDocker(distro, user, workRoot, profile string) *Docker {
	return &Docker{
		Cmd:      []string{"wsl.exe", "-d", distro, "-u", user, "--", "docker"},
		WorkRoot: workRoot,
		Mounts:   WSLDirs{Distro: distro, User: user, Root: "/var/lib/pool-agent/work"},
		Profile:  profile,
	}
}

// RequiredWSLConf lists the /etc/wsl.conf settings the agent requires
// (plan §8.5): no Windows drives mounted, no Windows interop.
var RequiredWSLConf = map[string]string{
	"automount.enabled":         "false",
	"automount.mountfstab":      "false",
	"interop.enabled":           "false",
	"interop.appendwindowspath": "false",
}

// CheckWSLConf parses wsl.conf content and returns every required setting
// that is missing or wrong. Keys are case-insensitive; values compare
// case-insensitively; inline "#" comments are ignored.
func CheckWSLConf(content string) []string {
	got := map[string]string{}
	section := ""
	sc := bufio.NewScanner(strings.NewReader(content))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if i := strings.IndexAny(line, "#;"); i >= 0 {
			line = strings.TrimSpace(line[:i])
		}
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.ToLower(strings.TrimSpace(line[1 : len(line)-1]))
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		got[section+"."+strings.ToLower(strings.TrimSpace(k))] = strings.ToLower(strings.Trim(strings.TrimSpace(v), `"`))
	}
	var problems []string
	for key, want := range RequiredWSLConf {
		if got[key] != want {
			problems = append(problems, fmt.Sprintf("%s must be %s (found %q)", key, want, got[key]))
		}
	}
	return problems
}

// VerifyWSLDistro reads /etc/wsl.conf from the distro and checks it. The
// agent calls this every time it starts (plan §8.5). UNTESTED.
func VerifyWSLDistro(ctx context.Context, distro string) error {
	out, err := exec.CommandContext(ctx, "wsl.exe", "-d", distro, "-u", "root", "--", "cat", "/etc/wsl.conf").Output()
	if err != nil {
		return fmt.Errorf("read wsl.conf in %s: %w", distro, err)
	}
	if problems := CheckWSLConf(string(out)); len(problems) > 0 {
		return errors.New("unsafe WSL distro configuration: " + strings.Join(problems, "; "))
	}
	return nil
}
