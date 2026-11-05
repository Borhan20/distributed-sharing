//go:build docker

// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

// Sandbox escape test suite (plan §10 "Security", Phase 1 exit criterion
// "a job cannot read host files, reach the LAN, or see Windows drives").
// Runs real containers through the sandbox package:
//
//	go test -tags docker ./sandbox/...
package sandbox

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"pool/agent/protocol"
)

var (
	imageOnce sync.Once
	image     string
	imageErr  error
)

// pinnedBusybox pulls busybox and returns it pinned by digest.
func pinnedBusybox(t *testing.T) string {
	t.Helper()
	if err := exec.Command("docker", "version", "--format", "{{.Server.Version}}").Run(); err != nil {
		t.Skip("docker is not available")
	}
	imageOnce.Do(func() {
		if err := exec.Command("docker", "image", "inspect", "busybox:1.36").Run(); err != nil {
			if out, err := exec.Command("docker", "pull", "-q", "busybox:1.36").CombinedOutput(); err != nil {
				imageErr = errors.New(string(out))
				return
			}
		}
		out, err := exec.Command("docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", "busybox:1.36").Output()
		if err != nil {
			imageErr = err
			return
		}
		image = strings.TrimSpace(string(out))
	})
	if imageErr != nil {
		t.Skipf("cannot get busybox image: %v", imageErr)
	}
	if !strings.Contains(image, "@sha256:") {
		t.Fatalf("image not pinned: %s", image)
	}
	return image
}

func randID(t *testing.T) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return "test-" + hex.EncodeToString(b)
}

type run struct {
	res    Result
	err    error
	stdout string
	stderr string
	rt     *Docker
	id     string
}

func runScript(t *testing.T, script string, mutate func(*Spec)) run {
	t.Helper()
	img := pinnedBusybox(t)
	rt := NewLocalDocker(t.TempDir(), "sandbox-test")
	s := Spec{
		AttemptID: randID(t), TaskID: "t", Epoch: 1, Group: "test", Profile: "sandbox-test", Tier: protocol.T1,
		Image: img, Command: []string{"sh", "-c", script}, CPU: 1, MemoryMB: 128, MaxRuntime: 90 * time.Second,
	}
	if mutate != nil {
		mutate(&s)
	}
	var mu sync.Mutex
	var out, errb strings.Builder
	started := false
	res, err := rt.Run(context.Background(), s, func() { started = true }, func(stream string, b []byte) {
		mu.Lock()
		defer mu.Unlock()
		if stream == "stdout" {
			out.Write(b)
		} else {
			errb.Write(b)
		}
	})
	if err == nil && !started {
		t.Fatal("onStart was not called")
	}
	t.Cleanup(func() { _ = rt.Cleanup(s.AttemptID) })
	return run{res: res, err: err, stdout: out.String(), stderr: errb.String(), rt: rt, id: s.AttemptID}
}

func TestDetect(t *testing.T) {
	pinnedBusybox(t)
	info, err := NewLocalDocker(t.TempDir(), "x").Detect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(info.Tiers) == 0 || info.Tiers[0] != protocol.T1 {
		t.Fatalf("tiers: %+v", info)
	}
	t.Logf("docker %s rootless=%v runsc=%v", info.ServerVersion, info.Rootless, info.Runsc)
}

func TestEscapeIdentityAndPrivileges(t *testing.T) {
	r := runScript(t, `id -u; id -g; grep -E '^(CapEff|CapPrm|CapBnd|NoNewPrivs):' /proc/self/status`, nil)
	if r.err != nil || r.res.ExitCode != 0 {
		t.Fatalf("run: %v exit=%d stderr=%s", r.err, r.res.ExitCode, r.stderr)
	}
	lines := strings.Fields(r.stdout)
	if len(lines) < 2 || lines[0] != "65534" || lines[1] != "65534" {
		t.Fatalf("container must run as 65534:65534, got %q", r.stdout)
	}
	for _, want := range []string{"CapEff:\t0000000000000000", "CapPrm:\t0000000000000000", "CapBnd:\t0000000000000000", "NoNewPrivs:\t1"} {
		if !strings.Contains(r.stdout, want) {
			t.Errorf("want %q in:\n%s", want, r.stdout)
		}
	}
}

func TestEscapeReadOnlyRootAndNoexecTmp(t *testing.T) {
	r := runScript(t, `
touch /pwned 2>/dev/null && echo ROOT_WRITABLE
touch /etc/pwned 2>/dev/null && echo ETC_WRITABLE
echo ok > /tmp/t && echo TMP_OK
cp /bin/busybox /tmp/bb 2>/dev/null && /tmp/bb true 2>/dev/null && echo TMP_EXEC
echo data > /out/written && echo OUT_OK
true`, nil)
	if r.err != nil {
		t.Fatal(r.err)
	}
	for _, bad := range []string{"ROOT_WRITABLE", "ETC_WRITABLE", "TMP_EXEC"} {
		if strings.Contains(r.stdout, bad) {
			t.Errorf("%s", bad)
		}
	}
	for _, good := range []string{"TMP_OK", "OUT_OK"} {
		if !strings.Contains(r.stdout, good) {
			t.Errorf("missing %s: %s %s", good, r.stdout, r.stderr)
		}
	}
}

func TestEscapeNetwork(t *testing.T) {
	r := runScript(t, `
cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' '
echo ---
nc -w 2 1.1.1.1 53 </dev/null >/dev/null 2>&1 && echo INTERNET_REACHABLE
wget -T 2 -q -O- http://169.254.169.254/ >/dev/null 2>&1 && echo METADATA_REACHABLE
wget -T 2 -q -O- http://192.168.1.1/ >/dev/null 2>&1 && echo LAN_REACHABLE
nslookup example.org >/dev/null 2>&1 && echo DNS_WORKS
true`, nil)
	if r.err != nil {
		t.Fatal(r.err)
	}
	ifaces, rest, _ := strings.Cut(r.stdout, "---")
	if strings.TrimSpace(ifaces) != "lo" {
		t.Errorf("want only the loopback interface, got %q", ifaces)
	}
	for _, bad := range []string{"INTERNET_REACHABLE", "METADATA_REACHABLE", "LAN_REACHABLE", "DNS_WORKS"} {
		if strings.Contains(rest, bad) {
			t.Errorf("%s", bad)
		}
	}
}

func TestEscapeHostFiles(t *testing.T) {
	marker := "pool-host-marker-" + randID(t)
	hostFile := filepath.Join(os.TempDir(), marker)
	if err := os.WriteFile(hostFile, []byte("host secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	defer os.Remove(hostFile)
	home, _ := os.UserHomeDir()
	r := runScript(t, `
find / -name '`+marker+`' 2>/dev/null | grep -v '^/proc' && echo HOST_FILE_VISIBLE
ls /var/run/docker.sock /run/docker.sock 2>/dev/null && echo DOCKER_SOCKET_VISIBLE
ls '`+home+`' 2>/dev/null && echo HOME_VISIBLE
ls /mnt/c /mnt/wsl 2>/dev/null && echo WINDOWS_DRIVES_VISIBLE
grep -c ' /out ' /proc/self/mountinfo
true`, nil)
	if r.err != nil {
		t.Fatal(r.err)
	}
	for _, bad := range []string{"HOST_FILE_VISIBLE", "DOCKER_SOCKET_VISIBLE", "HOME_VISIBLE", "WINDOWS_DRIVES_VISIBLE"} {
		if strings.Contains(r.stdout, bad) {
			t.Errorf("%s:\n%s", bad, r.stdout)
		}
	}
}

func TestPidsLimit(t *testing.T) {
	// The inner shell dies when fork fails; the outer one counts processes
	// with builtins only (no fork needed).
	r := runScript(t, `sh -c 'i=0; while [ $i -lt 100 ]; do sleep 30 & i=$((i+1)); done' 2>/dev/null; set -- /proc/[0-9]*; echo $#`,
		func(s *Spec) { s.PidsLimit = 32 })
	if r.err != nil {
		t.Fatal(r.err)
	}
	n := strings.TrimSpace(r.stdout)
	var count int
	for _, c := range n {
		if c >= '0' && c <= '9' {
			count = count*10 + int(c-'0')
		}
	}
	if count == 0 || count > 32 {
		t.Fatalf("pids limit 32 not enforced: %q processes", n)
	}
}

func TestMemoryLimit(t *testing.T) {
	r := runScript(t, `a=$(dd if=/dev/zero bs=1048576 count=200 2>/dev/null | tr '\000' 'a'); echo SURVIVED ${#a}`,
		func(s *Spec) { s.MemoryMB = 64 })
	if r.err != nil {
		t.Fatal(r.err)
	}
	if strings.Contains(r.stdout, "SURVIVED 209715200") || r.res.ExitCode == 0 {
		t.Fatalf("memory limit not enforced: exit=%d out=%q", r.res.ExitCode, r.stdout)
	}
}

func TestOutputsCollectedAndCleanedUp(t *testing.T) {
	r := runScript(t, `mkdir -p /out/sub && printf 'seed=3\n' > /out/result.txt && echo x > /out/sub/private &&
chmod 600 /out/sub/private && chmod 700 /out/sub && ln -s /etc/passwd /out/link && echo done`, nil)
	if r.err != nil || r.res.ExitCode != 0 {
		t.Fatalf("run: %v %d %s", r.err, r.res.ExitCode, r.stderr)
	}
	if len(r.res.Outputs) != 2 {
		t.Fatalf("outputs: %+v notes: %v", r.res.Outputs, r.res.Notes)
	}
	sum := sha256.Sum256([]byte("seed=3\n"))
	if r.res.Outputs[0].Path != "result.txt" || r.res.Outputs[0].SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("result.txt: %+v", r.res.Outputs[0])
	}
	if r.res.Outputs[1].Path != "sub/private" {
		t.Fatalf("private file not collected: %+v", r.res.Outputs)
	}
	if _, err := os.ReadFile(r.res.Outputs[1].LocalPath); err != nil {
		t.Fatalf("collected copy unreadable: %v", err)
	}
	if err := r.rt.Cleanup(r.id); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	if _, err := os.Stat(filepath.Join(r.rt.WorkRoot, "mounts", r.id)); !os.IsNotExist(err) {
		t.Fatalf("mount dir still exists: %v", err)
	}
	if out, _ := exec.Command("docker", "ps", "-aq", "--filter", "name="+ContainerName(r.id)).Output(); len(strings.TrimSpace(string(out))) != 0 {
		t.Fatal("container was not removed")
	}
}

func TestMaxRuntime(t *testing.T) {
	start := time.Now()
	r := runScript(t, `sleep 60`, func(s *Spec) { s.MaxRuntime = 2 * time.Second })
	if r.err != nil {
		t.Fatal(r.err)
	}
	if !r.res.TimedOut || r.res.ExitCode == 0 || time.Since(start) > 40*time.Second {
		t.Fatalf("max runtime not enforced: %+v after %v", r.res, time.Since(start))
	}
}

func TestKillAndKillAll(t *testing.T) {
	img := pinnedBusybox(t)
	rt := NewLocalDocker(t.TempDir(), "kill-test")
	ids := []string{randID(t), randID(t)}
	var wg sync.WaitGroup
	errs := make([]error, len(ids))
	startedCh := make(chan struct{}, len(ids))
	for i, id := range ids {
		wg.Add(1)
		go func(i int, id string) {
			defer wg.Done()
			_, errs[i] = rt.Run(context.Background(), Spec{
				AttemptID: id, Profile: "kill-test", Group: "g", Tier: protocol.T1, Image: img,
				Command: []string{"sleep", "120"}, CPU: 0.5, MemoryMB: 32,
			}, func() { startedCh <- struct{}{} }, nil)
		}(i, id)
	}
	for range ids {
		select {
		case <-startedCh:
		case <-time.After(60 * time.Second):
			t.Fatal("containers did not start")
		}
	}
	if err := rt.Kill(context.Background(), ids[0]); err != nil {
		t.Fatal(err)
	}
	n, err := rt.KillAll(context.Background(), "kill-test")
	if err != nil || n < 1 {
		t.Fatalf("KillAll: n=%d err=%v", n, err)
	}
	wg.Wait()
	for i, err := range errs {
		if !errors.Is(err, ErrKilled) {
			t.Errorf("attempt %d: err = %v, want ErrKilled", i, err)
		}
		_ = rt.Cleanup(ids[i])
	}
}

func TestPauseUnpauseThrottle(t *testing.T) {
	img := pinnedBusybox(t)
	rt := NewLocalDocker(t.TempDir(), "pause-test")
	id := randID(t)
	done := make(chan Result, 1)
	started := make(chan struct{})
	go func() {
		res, _ := rt.Run(context.Background(), Spec{AttemptID: id, Profile: "pause-test", Group: "g", Tier: protocol.T1,
			Image: img, Command: []string{"sleep", "3"}, CPU: 1, MemoryMB: 32}, func() { close(started) }, nil)
		done <- res
	}()
	<-started
	ctx := context.Background()
	if err := rt.Throttle(ctx, id, 0.25); err != nil {
		t.Fatalf("throttle: %v", err)
	}
	if err := rt.Pause(ctx, id); err != nil {
		t.Fatalf("pause: %v", err)
	}
	time.Sleep(4 * time.Second)
	select {
	case <-done:
		t.Fatal("paused container finished")
	default:
	}
	if err := rt.Unpause(ctx, id); err != nil {
		t.Fatalf("unpause: %v", err)
	}
	select {
	case res := <-done:
		if res.ExitCode != 0 {
			t.Fatalf("exit %d", res.ExitCode)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("container did not finish after unpause")
	}
	_ = rt.Cleanup(id)
}

func TestTaskNumberAndImageDefaultCommand(t *testing.T) {
	r := runScript(t, `echo "part=$POOL_TASK_INDEX of=$POOL_TASK_COUNT"`, func(s *Spec) { s.TaskIndex, s.TaskCount = 3, 8 })
	if r.err != nil || r.res.ExitCode != 0 || strings.TrimSpace(r.stdout) != "part=3 of=8" {
		t.Fatalf("task number inside the container: err=%v exit=%d out=%q", r.err, r.res.ExitCode, r.stdout)
	}
	// busybox's default CMD is "sh", which exits 0 at once without a terminal.
	r = runScript(t, "", func(s *Spec) { s.Command = nil })
	if r.err != nil || r.res.ExitCode != 0 {
		t.Fatalf("image default command: err=%v exit=%d stderr=%s", r.err, r.res.ExitCode, r.stderr)
	}
}

// The run is measured (docs/design/pool-decides.md §4.2): memory, CPU time, run time, outputs, image size.
func TestUsageIsMeasured(t *testing.T) {
	// About 100 MiB held for 4 s while one core is busy, then a 3-byte output file.
	r := runScript(t, `a=$(dd if=/dev/zero bs=1048576 count=100 2>/dev/null | tr '\000' 'a');
		end=$(( $(date +%s) + 4 )); while [ $(date +%s) -lt $end ]; do :; done; echo ${#a} >/dev/null; echo hi > /out/x`,
		func(s *Spec) { s.MemoryMB = 512 })
	if r.err != nil || r.res.ExitCode != 0 {
		t.Fatalf("err=%v exit=%d stderr=%q", r.err, r.res.ExitCode, r.stderr)
	}
	u := r.res.Usage
	if u == nil {
		t.Fatal("no usage")
	}
	t.Logf("usage: %+v", *u)
	if u.Samples < 2 || u.PeakMemoryMB < 90 || u.PeakMemoryMB > 512 {
		t.Errorf("peak memory %d MiB from %d samples, want about 100", u.PeakMemoryMB, u.Samples)
	}
	// One core is busy the whole time, so CPU time should be close to the run time (design: within 10-25%).
	if u.CPUSeconds < u.WallSeconds*0.75 || u.CPUSeconds > u.WallSeconds*1.15 || u.PeakCPU < 0.5 || u.PeakCPU > 1.3 {
		t.Errorf("cpu %.2f s, peak %.2f cores over %.1f s; want about 1 core busy", u.CPUSeconds, u.PeakCPU, u.WallSeconds)
	}
	if u.WallSeconds < 4 || u.OutputBytes != 3 || u.ImageMB < 1 || u.OOMKilled {
		t.Errorf("got %+v", *u)
	}
}

// Docker's out-of-memory mark reaches the result, so pool can tell "needs more memory" from a crash.
func TestOutOfMemoryIsReported(t *testing.T) {
	r := runScript(t, `a=$(dd if=/dev/zero bs=1048576 count=200 2>/dev/null | tr '\000' 'a'); echo ${#a}`,
		func(s *Spec) { s.MemoryMB = 64 })
	if r.err != nil {
		t.Fatal(r.err)
	}
	if r.res.ExitCode == 0 || r.res.Usage == nil || !r.res.Usage.OOMKilled {
		t.Fatalf("exit=%d usage=%+v", r.res.ExitCode, r.res.Usage)
	}
}
