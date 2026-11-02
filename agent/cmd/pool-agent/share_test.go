package main

import (
	"bytes"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"pool/agent/enroll"
	"pool/agent/policy"
)

func TestParseWithGroupAcceptsFlagsOnEitherSide(t *testing.T) {
	for _, args := range [][]string{
		{"lab", "--gpu", "--memory", "8Gi"},
		{"--gpu", "--memory", "8Gi", "lab"},
		{"--gpu", "lab", "--memory=8Gi"},
	} {
		fs := flag.NewFlagSet("share", flag.ContinueOnError)
		gpu := fs.Bool("gpu", false, "")
		mem := fs.String("memory", "", "")
		group, err := parseWithGroup(fs, args)
		if err != nil || group != "lab" || !*gpu || *mem != "8Gi" {
			t.Errorf("%v: group=%q gpu=%v mem=%q err=%v", args, group, *gpu, *mem, err)
		}
	}
	for _, args := range [][]string{{}, {"--gpu"}, {"lab", "other"}} {
		fs := flag.NewFlagSet("share", flag.ContinueOnError)
		fs.SetOutput(new(bytes.Buffer))
		fs.Bool("gpu", false, "")
		if _, err := parseWithGroup(fs, args); err == nil {
			t.Errorf("%v: expected an error", args)
		}
	}
}

// fakeProfile creates an enrolled-looking profile in a temporary agent home.
func fakeProfile(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("POOL_AGENT_HOME", home)
	dir, err := enroll.ProfileDir(home, "default")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := enroll.GenerateKey(filepath.Join(dir, "device.key")); err != nil {
		t.Fatal(err)
	}
	p := &enroll.Profile{Name: "default", Server: "http://127.0.0.1:8000", DeviceID: "test-device",
		InsecureHTTP: true, EnrolledAt: time.Now(), Dir: dir, CertPEM: []byte("x"), CAPEM: []byte("x")}
	if err := p.Save(); err != nil {
		t.Fatal(err)
	}
	return filepath.Join(dir, enroll.PolicyFile)
}

func TestShareAndUnshareCommands(t *testing.T) {
	path := fakeProfile(t)
	var out bytes.Buffer
	if err := cmdShare([]string{"ml-test-lab", "--memory", "6Gi"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(out.String(), `Shared this computer with "ml-test-lab": up to 50% CPU, 6Gi memory, 20Gi disk, no GPU, at any time.`) {
		t.Errorf("unexpected output:\n%s", out.String())
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Errorf("policy mode = %v, want 0600", st.Mode().Perm())
	}
	p, err := policy.Load(path)
	if err != nil || p.Find("ml-test-lab") == nil {
		t.Fatalf("policy after share: %v", err)
	}

	out.Reset()
	if err := cmdShare([]string{"--cpu-percent", "25", "ml-test-lab"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(out.String(), `Updated sharing with "ml-test-lab": up to 25% CPU, 6Gi memory`) {
		t.Errorf("unexpected output:\n%s", out.String())
	}

	out.Reset()
	if err := cmdUnshare([]string{"ml-test-lab"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(out.String(), `Stopped sharing this computer with "ml-test-lab".`) {
		t.Errorf("unexpected output:\n%s", out.String())
	}
	out.Reset()
	if err := cmdUnshare([]string{"ml-test-lab"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "nothing changed") {
		t.Errorf("unexpected output:\n%s", out.String())
	}
	if p, _ := policy.Load(path); len(p.Shares) != 0 || len(p.Priority) != 0 {
		t.Errorf("policy not empty after unshare: %+v", p)
	}
	leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".policy-*"))
	if len(leftovers) != 0 {
		t.Errorf("temporary files left behind: %v", leftovers)
	}
}

func TestShareNeedsEnrollment(t *testing.T) {
	t.Setenv("POOL_AGENT_HOME", t.TempDir())
	err := cmdShare([]string{"lab"}, new(bytes.Buffer))
	if err == nil || !strings.Contains(err.Error(), "not enrolled yet") {
		t.Fatalf("err = %v", err)
	}
}
