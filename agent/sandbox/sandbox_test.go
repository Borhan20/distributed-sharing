// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package sandbox

import (
	"archive/tar"
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"pool/agent/protocol"
)

func spec() Spec {
	return Spec{
		AttemptID: "att-1", TaskID: "task-1", Epoch: 3, Group: "research-team", Profile: "default",
		Tier: protocol.T1, Image: "busybox@sha256:" + strings.Repeat("a", 64),
		Command: []string{"sh", "-c", "echo hi"}, Env: map[string]string{"SEED": "3"},
		CPU: 1.5, MemoryMB: 256,
	}
}

func has(args []string, seq ...string) bool {
	for i := 0; i+len(seq) <= len(args); i++ {
		ok := true
		for j := range seq {
			if args[i+j] != seq[j] {
				ok = false
				break
			}
		}
		if ok {
			return true
		}
	}
	return false
}

func TestCreateArgsHardening(t *testing.T) {
	args, err := CreateArgs(spec(), "/work/att-1/out")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range [][]string{
		{"--user", "65534:65534"}, {"--cap-drop", "ALL"}, {"--security-opt", "no-new-privileges"},
		{"--read-only"}, {"--network", "none"}, {"--pids-limit", "512"}, {"--memory", "256m"},
		{"--memory-swap", "256m"}, {"--cpus", "1.5"}, {"--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"},
		{"--mount", "type=bind,src=/work/att-1/out,dst=/out"}, {"--label", "pool.agent=1"},
		{"--label", "pool.attempt=att-1"}, {"--label", "pool.group=research-team"}, {"--label", "pool.profile=default"},
		{"--env", "SEED=3"}, {"--env", "POOL_ATTEMPT_ID=att-1"}, {"--env", "POOL_EPOCH=3"},
		{"--entrypoint", "sh", "busybox@sha256:" + strings.Repeat("a", 64), "-c", "echo hi"},
	} {
		if !has(args, want...) {
			t.Errorf("missing %v in %v", want, args)
		}
	}
	for _, bad := range []string{"--privileged", "--runtime", "--device", "--network=host", "-v"} {
		if has(args, bad) {
			t.Errorf("unexpected %s", bad)
		}
	}
	// Only one mount: the outputs directory.
	n := 0
	for _, a := range args {
		if a == "--mount" || a == "-v" || a == "--volume" {
			n++
		}
	}
	if n != 1 {
		t.Errorf("want exactly one mount, got %d", n)
	}
}

func TestCreateArgsTiersAndGPU(t *testing.T) {
	s := spec()
	s.Tier = protocol.T2
	s.GPUs = 1
	args, err := CreateArgs(s, "/o")
	if err != nil {
		t.Fatal(err)
	}
	if !has(args, "--runtime", "runsc") || !has(args, "--device", "nvidia.com/gpu=all") {
		t.Fatalf("T2/GPU flags missing: %v", args)
	}
	for _, tier := range []protocol.Tier{protocol.T0, protocol.T3, "T9"} {
		s.Tier = tier
		if _, err := CreateArgs(s, "/o"); err == nil {
			t.Errorf("tier %s accepted by the docker runtime", tier)
		}
	}
}

func TestCreateArgsRejects(t *testing.T) {
	cases := map[string]func(*Spec){
		"reserved env":      func(s *Spec) { s.Env = map[string]string{"POOL_EPOCH": "9"} },
		"bad env name":      func(s *Spec) { s.Env = map[string]string{"A=B": "1"} },
		"nul in env":        func(s *Spec) { s.Env = map[string]string{"A": "x\x00y"} },
		"relative out path": func(s *Spec) { s.OutputsPath = "out" },
		"dotdot out path":   func(s *Spec) { s.OutputsPath = "/out/../etc" },
		"comma out path":    func(s *Spec) { s.OutputsPath = "/out,dst=/etc" },
		"no memory":         func(s *Spec) { s.MemoryMB = 0 },
		"no command":        func(s *Spec) { s.Command = nil },
	}
	for name, mutate := range cases {
		s := spec()
		mutate(&s)
		if _, err := CreateArgs(s, "/o"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := CreateArgs(spec(), "/o,dst=/etc"); err == nil {
		t.Error("mount path with comma accepted")
	}
}

func makeTar(t *testing.T, entries []tar.Header, bodies map[string]string) *bytes.Buffer {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, h := range entries {
		body := bodies[h.Name]
		h.Size = int64(len(body))
		if h.Typeflag != tar.TypeReg {
			h.Size = 0
		}
		if h.Mode == 0 {
			h.Mode = 0o600
		}
		if err := tw.WriteHeader(&h); err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			_, _ = tw.Write([]byte(body))
		}
	}
	_ = tw.Close()
	return &buf
}

func TestCollectTar(t *testing.T) {
	buf := makeTar(t, []tar.Header{
		{Name: "out/", Typeflag: tar.TypeDir},
		{Name: "out/result.txt", Typeflag: tar.TypeReg},
		{Name: "out/sub/", Typeflag: tar.TypeDir},
		{Name: "out/sub/a.bin", Typeflag: tar.TypeReg},
		{Name: "out/link", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"},
		{Name: "out/../../escape", Typeflag: tar.TypeReg},
	}, map[string]string{"out/result.txt": "seed=3\n", "out/sub/a.bin": "", "out/../../escape": "x"})
	dest := t.TempDir()
	outs, notes, err := collectTar(buf, dest, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(outs) != 2 || outs[0].Path != "result.txt" || outs[1].Path != "sub/a.bin" {
		t.Fatalf("outputs: %+v", outs)
	}
	// sha256("seed=3\n")
	if outs[0].Size != 7 || len(outs[0].SHA256) != 64 || !strings.HasPrefix(outs[0].LocalPath, dest) {
		t.Fatalf("output 0: %+v", outs[0])
	}
	if outs[1].SHA256 != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatalf("empty file hash: %s", outs[1].SHA256)
	}
	if len(notes) != 1 || !strings.Contains(notes[0], "link") {
		t.Fatalf("notes: %v", notes)
	}
}

func TestCollectTarCaps(t *testing.T) {
	var entries []tar.Header
	bodies := map[string]string{}
	for i := 0; i < MaxOutputFiles+5; i++ {
		name := "out/f" + strings.Repeat("x", i%3) + string(rune('a'+i%26)) + "-" + strconv.Itoa(i)
		entries = append(entries, tar.Header{Name: name, Typeflag: tar.TypeReg})
		bodies[name] = "1"
	}
	outs, notes, err := collectTar(makeTar(t, entries, bodies), t.TempDir(), 0)
	if err != nil || len(outs) != MaxOutputFiles || len(notes) != 1 {
		t.Fatalf("count cap: %d outputs, notes %v, err %v", len(outs), notes, err)
	}
	outs, notes, _ = collectTar(makeTar(t, entries[:10], bodies), t.TempDir(), 5)
	if len(outs) != 5 || len(notes) != 1 {
		t.Fatalf("size cap: %d outputs, notes %v", len(outs), notes)
	}
}

func TestCheckWSLConf(t *testing.T) {
	good := `[automount]
enabled = false        # do not mount C:\, D:\ ... into the distro
mountFsTab = false

[interop]
enabled = false        # cannot start Windows programs
appendWindowsPath = false
`
	if p := CheckWSLConf(good); len(p) != 0 {
		t.Fatalf("plan §8.5 config rejected: %v", p)
	}
	bad := strings.Replace(good, "enabled = false        # cannot", "enabled = true # cannot", 1)
	if p := CheckWSLConf(bad); len(p) != 1 || !strings.Contains(p[0], "interop.enabled") {
		t.Fatalf("interop on not detected: %v", p)
	}
	if p := CheckWSLConf(""); len(p) != len(RequiredWSLConf) {
		t.Fatalf("empty config: %v", p)
	}
}

// A stand-in "docker" records its arguments and the config it was given, so
// the test sees exactly what a real pull would receive.
func TestPrivatePullUsesAThrowawayConfig(t *testing.T) {
	work := t.TempDir()
	logf := filepath.Join(t.TempDir(), "calls.log")
	script := filepath.Join(t.TempDir(), "docker")
	body := "#!/bin/sh\necho \"ARGS $*\" >> " + logf + "\ncat \"$2/config.json\" >> " + logf + "\necho >> " + logf + "\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	d := &Docker{Cmd: []string{script}, WorkRoot: work}
	image := "ghcr.io/lab/app@sha256:" + strings.Repeat("b", 64)
	if err := d.pullPrivate(context.Background(), image, RegistryAuth{Key: "ghcr.io", Token: "tok-123"}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(logf)
	if err != nil {
		t.Fatal(err)
	}
	log := string(raw)
	if !strings.Contains(log, "pull --quiet "+image) || !strings.Contains(log, "ARGS --config "+work) {
		t.Fatalf("docker was called as: %s", log)
	}
	if !strings.Contains(log, `{"auths":{"ghcr.io":{"registrytoken":"tok-123"}}}`) {
		t.Fatalf("config given to docker: %s", log)
	}
	if strings.Contains(strings.SplitN(log, "\n", 2)[0], "tok-123") {
		t.Fatal("the token must not appear on the command line")
	}
	left, _ := os.ReadDir(work)
	if len(left) != 0 {
		t.Fatalf("throwaway config left behind: %v", left)
	}
	wsl := &Docker{Cmd: []string{"wsl.exe", "-d", "pool-runtime", "--", "docker"}, WorkRoot: work}
	if err := wsl.pullPrivate(context.Background(), image, RegistryAuth{Key: "ghcr.io", Token: "x"}); err == nil {
		t.Fatal("WSL2 runtime should refuse private pulls for now")
	}
}
