// Command pool-agent runs jobs from trusted groups on this computer under the
// owner's local policy (plan §5.1, §6.4).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sync"
	"syscall"
	"text/tabwriter"
	"time"

	"pool/agent/enroll"
	"pool/agent/policy"
	"pool/agent/sandbox"
	"pool/agent/session"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "0.1.0"

const usage = `pool-agent: share this computer with trusted groups (pool, protocol v1alpha1)

Usage:
  pool-agent enroll --server URL --code CODE [--profile NAME] [--name DEVICE-NAME] [--server-ca FILE] [--insecure-http]
  pool-agent run    [--profile NAME | --all] [--policy FILE]
  pool-agent kill       stop every job now and refuse new work (kill switch)
  pool-agent resume     release the kill switch
  pool-agent status     show what is running on this computer and who sent it
  pool-agent version

Environment:
  POOL_AGENT_HOME   agent home (default ~/.config/pool-agent on Linux, %ProgramData%\pool-agent on Windows)
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "enroll":
		err = cmdEnroll(os.Args[2:])
	case "run":
		err = cmdRun(os.Args[2:])
	case "kill":
		err = cmdKill(os.Args[2:])
	case "resume":
		err = cmdResume(os.Args[2:])
	case "status":
		err = cmdStatus(os.Args[2:], os.Stdout)
	case "version", "--version", "-v":
		fmt.Printf("pool-agent %s (%s/%s, protocol v1alpha1)\n", version, runtime.GOOS, runtime.GOARCH)
	case "help", "--help", "-h":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n%s", os.Args[1], usage)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func cmdEnroll(args []string) error {
	fs := flag.NewFlagSet("enroll", flag.ContinueOnError)
	server := fs.String("server", "", "control plane URL, e.g. https://pool.example.org")
	code := fs.String("code", "", "one-time enrollment code shown in the web UI (valid 10 minutes)")
	profile := fs.String("profile", "default", "profile name (one per server)")
	name := fs.String("name", "", "device name shown to group members (default: <os>-<random>)")
	serverCA := fs.String("server-ca", "", "PEM file with the CA that signed the server's TLS certificate")
	insecure := fs.Bool("insecure-http", false, "allow plain http, only for localhost dev servers")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *server == "" || *code == "" {
		return errors.New("enroll needs --server and --code")
	}
	home, err := enroll.Home()
	if err != nil {
		return err
	}
	if *serverCA != "" {
		if *serverCA, err = filepath.Abs(*serverCA); err != nil {
			return err
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	p, err := enroll.Enroll(ctx, enroll.Options{
		Home: home, Profile: *profile, Server: *server, Code: *code, Name: *name, InsecureHTTP: *insecure,
		ServerCAFile: *serverCA, AgentVersion: version,
	})
	if err != nil {
		return err
	}
	polPath := filepath.Join(p.Dir, enroll.PolicyFile)
	wrote, err := policy.WriteDefault(polPath)
	if err != nil {
		return err
	}
	fmt.Printf("Enrolled device %s with %s (profile %q).\n", p.DeviceID, p.Server, p.Name)
	fmt.Printf("Certificate valid until %s; it renews automatically while the agent runs.\n", p.CertNotAfter.Format(time.RFC3339))
	if wrote {
		fmt.Printf("\nThis device shares NOTHING yet. To share it, edit the local policy:\n  %s\n", polPath)
	} else {
		fmt.Printf("\nUsing the existing local policy: %s\n", polPath)
	}
	fmt.Printf("Then start the agent:  pool-agent run --profile %s\n", p.Name)
	return nil
}

func newLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
}

func cmdRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	profile := fs.String("profile", "default", "profile to run")
	all := fs.Bool("all", false, "run every enrolled profile (plan §7.11)")
	polPath := fs.String("policy", "", "policy file (default <home>/profiles/<profile>/policy.yaml)")
	wslDistro := fs.String("wsl-distro", "pool-runtime", "Windows only: dedicated WSL2 distro (UNTESTED)")
	wslUser := fs.String("wsl-user", "pool", "Windows only: user that owns the rootless engine in the distro (UNTESTED)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *all && *polPath != "" {
		return errors.New("--policy cannot be combined with --all; each profile uses its own policy")
	}
	home, err := enroll.Home()
	if err != nil {
		return err
	}
	names := []string{*profile}
	if *all {
		if names, err = enroll.ListProfiles(home); err != nil {
			return err
		}
		if len(names) == 0 {
			return errors.New("no enrolled profiles; run pool-agent enroll first")
		}
	}
	log := newLogger()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if runtime.GOOS == "windows" {
		if err := sandbox.VerifyWSLDistro(ctx, *wslDistro); err != nil {
			return err
		}
	}
	capacity := session.NewCapacity()
	var agents []*session.Agent
	for _, name := range names {
		p, err := enroll.Load(home, name)
		if err != nil {
			return err
		}
		if _, err := enroll.CheckServerURL(p.Server, p.InsecureHTTP); err != nil {
			return fmt.Errorf("profile %s: %w", name, err)
		}
		workDir := filepath.Join(p.Dir, enroll.WorkDir)
		if err := os.MkdirAll(workDir, 0o700); err != nil {
			return err
		}
		var rt sandbox.Runtime
		if runtime.GOOS == "windows" {
			rt = sandbox.NewWSLDocker(*wslDistro, *wslUser, workDir, name)
		} else {
			rt = sandbox.NewLocalDocker(workDir, name)
		}
		pp := *polPath
		if pp == "" {
			pp = filepath.Join(p.Dir, enroll.PolicyFile)
			if _, err := policy.WriteDefault(pp); err != nil {
				return err
			}
		}
		a, err := session.New(session.Config{
			Profile: p, Home: home, PolicyPath: pp, Runtime: rt, Capacity: capacity, AgentVersion: version, Logger: log,
		})
		if err != nil {
			return fmt.Errorf("profile %s: %w", name, err)
		}
		agents = append(agents, a)
	}
	log.Info("pool-agent starting", "version", version, "profiles", names, "home", home)
	var wg sync.WaitGroup
	errs := make(chan error, len(agents))
	for _, a := range agents {
		wg.Add(1)
		go func(a *session.Agent) {
			defer wg.Done()
			if err := a.Run(ctx); err != nil {
				errs <- err
				stop()
			}
		}(a)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		return err
	}
	log.Info("pool-agent stopped")
	return nil
}

func cmdKill(args []string) error {
	fs := flag.NewFlagSet("kill", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return err
	}
	home, err := enroll.Home()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(home, enroll.KillFile), []byte(time.Now().UTC().Format(time.RFC3339)+"\n"), 0o600); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	var rt sandbox.Runtime = sandbox.NewLocalDocker(filepath.Join(home, "kill"), "")
	if runtime.GOOS == "windows" {
		rt = sandbox.NewWSLDocker("pool-runtime", "pool", filepath.Join(home, "kill"), "")
	}
	n, kerr := rt.KillAll(ctx, "")
	fmt.Printf("Kill switch ON. Removed %d container(s). No new work will start until: pool-agent resume\n", n)
	if kerr != nil {
		return fmt.Errorf("removing containers: %w (the running agent also stops them within a second)", kerr)
	}
	return nil
}

func cmdResume(args []string) error {
	fs := flag.NewFlagSet("resume", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return err
	}
	home, err := enroll.Home()
	if err != nil {
		return err
	}
	err = os.Remove(filepath.Join(home, enroll.KillFile))
	if errors.Is(err, os.ErrNotExist) {
		fmt.Println("Kill switch was not on.")
		return nil
	}
	if err != nil {
		return err
	}
	fmt.Println("Kill switch released. The agent will accept work again under the local policy.")
	return nil
}

func cmdStatus(args []string, w io.Writer) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return err
	}
	home, err := enroll.Home()
	if err != nil {
		return err
	}
	_, kerr := os.Stat(filepath.Join(home, enroll.KillFile))
	fmt.Fprintf(w, "Home: %s\nKill switch: %s\n", home, onOff(kerr == nil))
	names, err := enroll.ListProfiles(home)
	if err != nil {
		return err
	}
	if len(names) == 0 {
		fmt.Fprintln(w, "No enrolled profiles.")
		return nil
	}
	for _, name := range names {
		p, err := enroll.Load(home, name)
		if err != nil {
			fmt.Fprintf(w, "\nProfile %s: %v\n", name, err)
			continue
		}
		fmt.Fprintf(w, "\nProfile %s  server %s  device %s  cert until %s\n", name, p.Server, p.DeviceID,
			p.CertNotAfter.Local().Format(time.RFC3339))
		st, err := session.ReadState(p.Dir)
		if err != nil {
			fmt.Fprintln(w, "  agent has not run yet")
			continue
		}
		fmt.Fprintf(w, "  connected: %v (as of %s)\n", st.Connected, st.UpdatedAt.Local().Format(time.RFC3339))
		if len(st.Attempts) == 0 {
			fmt.Fprintln(w, "  nothing running")
			continue
		}
		tw := tabwriter.NewWriter(w, 2, 4, 2, ' ', 0)
		fmt.Fprintln(tw, "  ATTEMPT\tGROUP\tSUBMITTER\tTIER\tSTATE\tSTARTED\tIMAGE")
		for _, a := range st.Attempts {
			started := "-"
			if a.StartedAt != nil {
				started = a.StartedAt.Local().Format("15:04:05")
			}
			state := a.State
			if a.OwnerAction != "" {
				state += " (" + a.OwnerAction + ")"
			}
			fmt.Fprintf(tw, "  %s\t%s\t%s\t%s\t%s\t%s\t%s\n", a.AttemptID, a.Group, a.Submitter, a.Tier, state, started, a.Image)
		}
		_ = tw.Flush()
	}
	return nil
}

func onOff(b bool) string {
	if b {
		return "ON (no work runs; release with: pool-agent resume)"
	}
	return "off"
}
