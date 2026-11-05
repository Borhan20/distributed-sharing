package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"pool/agent/enroll"
	"pool/agent/policy"
	"pool/agent/sysinfo"
)

// parseWithGroup parses flags that may come before or after the single GROUP
// argument ("share lab --gpu" and "share --gpu lab" both work).
func parseWithGroup(fs *flag.FlagSet, args []string) (string, error) {
	if err := fs.Parse(args); err != nil {
		return "", err
	}
	if fs.NArg() == 0 {
		return "", errors.New("missing GROUP (the group's short name, as shown in the web UI)")
	}
	group := fs.Arg(0)
	if err := fs.Parse(fs.Args()[1:]); err != nil {
		return "", err
	}
	if fs.NArg() > 0 {
		return "", fmt.Errorf("unexpected argument %q (give one GROUP at a time)", fs.Arg(0))
	}
	return group, nil
}

// policyFor finds the policy file of an enrolled profile.
func policyFor(profile, override string) (string, error) {
	home, err := enroll.Home()
	if err != nil {
		return "", err
	}
	p, err := enroll.Load(home, profile)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("not enrolled yet (profile %q): run pool-agent enroll first", profile)
		}
		return "", err
	}
	if override != "" {
		return override, nil
	}
	return filepath.Join(p.Dir, enroll.PolicyFile), nil
}

func readPolicy(path string) ([]byte, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return []byte(policy.DefaultYAML), nil
	}
	return raw, err
}

// writeAtomic replaces path with data (mode 0600) so the running agent never
// reads a half-written policy.
func writeAtomic(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".policy-*.yaml")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp) // no-op after a successful rename
	if err := f.Chmod(0o600); err != nil {
		f.Close()
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func cmdShare(args []string, w io.Writer) error {
	fs := flag.NewFlagSet("share", flag.ContinueOnError)
	profile := fs.String("profile", "default", "profile (one per server)")
	polPath := fs.String("policy", "", "policy file (default <home>/profiles/<profile>/policy.yaml)")
	cpu := fs.Int("cpu-percent", policy.DefaultSharePercent, "share of this computer's CPU, 1-100 (new share default 50)")
	mem := fs.String("memory", "", "most memory the group may use, e.g. 8Gi (new share default: half of this computer's RAM)")
	disk := fs.String("disk", policy.DefaultShareDisk, "most disk space the group may use")
	gpu := fs.Bool("gpu", false, "allow the group to use this computer's GPU (--gpu=false to turn off)")
	idle := fs.String("only-when-idle", "", "run only after this long without keyboard or mouse use, e.g. 10m")
	apps := fs.Bool("allow-apps", false, "allow apps: programs that listen on a port, reached only through pool "+
		"(they get no network; --allow-apps=false to turn off)")
	appHours := fs.Float64("app-hours", policy.DefaultAppHours, "the longest one app may run here, in hours")
	group, err := parseWithGroup(fs, args)
	if err != nil {
		return err
	}
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	e := policy.ShareEdit{Group: group}
	if set["cpu-percent"] {
		e.CPUPercent = cpu
	}
	if set["memory"] {
		e.Memory = mem
	}
	if set["disk"] {
		e.Disk = disk
	}
	if set["gpu"] {
		e.GPU = gpu
	}
	if set["only-when-idle"] {
		e.OnlyWhenIdle = idle
	}
	if set["allow-apps"] {
		e.AllowApps = apps
	}
	if set["app-hours"] {
		e.AppHours = appHours
	}

	path, err := policyFor(*profile, *polPath)
	if err != nil {
		return err
	}
	raw, err := readPolicy(path)
	if err != nil {
		return err
	}
	out, created, err := policy.AddOrUpdateShare(raw, e, policy.ShareDefaults{TotalMemoryMB: sysinfo.TotalMemoryMB()})
	if err != nil {
		return err
	}
	if err := writeAtomic(path, out); err != nil {
		return err
	}
	p, err := policy.Parse(out)
	if err != nil { // AddOrUpdateShare already validated; this cannot fail
		return err
	}
	verb := "Shared this computer with"
	if !created {
		verb = "Updated sharing with"
	}
	fmt.Fprintf(w, "%s %q: %s.\n", verb, group, p.Find(group).Describe())
	fmt.Fprintln(w, "The running agent applies this within a few seconds. The server lists the computer in the group "+
		"automatically if you are a member of it.")
	fmt.Fprintf(w, "Policy file: %s\n", path)
	return nil
}

func cmdUnshare(args []string, w io.Writer) error {
	fs := flag.NewFlagSet("unshare", flag.ContinueOnError)
	profile := fs.String("profile", "default", "profile (one per server)")
	polPath := fs.String("policy", "", "policy file (default <home>/profiles/<profile>/policy.yaml)")
	group, err := parseWithGroup(fs, args)
	if err != nil {
		return err
	}
	path, err := policyFor(*profile, *polPath)
	if err != nil {
		return err
	}
	raw, err := readPolicy(path)
	if err != nil {
		return err
	}
	out, removed, err := policy.RemoveShare(raw, group)
	if err != nil {
		return err
	}
	if !removed {
		fmt.Fprintf(w, "This computer was not shared with %q; nothing changed.\n", group)
		return nil
	}
	if err := writeAtomic(path, out); err != nil {
		return err
	}
	fmt.Fprintf(w, "Stopped sharing this computer with %q. The running agent stops that group's jobs within a few "+
		"seconds and takes no new work from it.\n", group)
	fmt.Fprintf(w, "Policy file: %s\n", path)
	return nil
}
