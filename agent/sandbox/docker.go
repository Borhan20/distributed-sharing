// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package sandbox

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"pool/agent/protocol"
)

// Docker runs attempts with the `docker` CLI (no shell is involved; every
// argument is passed separately). The Engine Go SDK module
// github.com/moby/moby/client needs Go 1.24, newer than this project's Go
// 1.23, so the CLI is used for now (see agent/README.md).
type Docker struct {
	// Cmd is the docker command prefix, e.g. ["docker"], or for WSL2
	// ["wsl.exe", "-d", "pool-runtime", "--", "docker"].
	Cmd []string
	// WorkRoot is the agent-owned directory for collected outputs (0700).
	WorkRoot string
	// Mounts creates and removes the per-attempt output directory that is
	// bind-mounted into the container.
	Mounts OutDirs
	// Profile is used to label containers.
	Profile string

	mu      sync.Mutex
	images  map[string]string // attempt id -> image (for cleanup)
	outDirs map[string]string // attempt id -> mount dir
}

// OutDirs manages output directories visible to the Docker daemon.
type OutDirs interface {
	// Prepare creates <root>/<attempt>/out with mode 0777 inside a 0700
	// parent and returns the out path as seen by the daemon.
	Prepare(attemptID string) (string, error)
	// Remove deletes the attempt directory. It may fail for files the
	// container's user created; the caller then falls back to a cleanup
	// container.
	Remove(attemptID string) error
}

// LocalDirs is OutDirs on the local file system (Linux).
type LocalDirs struct{ Root string }

func (l LocalDirs) Prepare(attemptID string) (string, error) {
	dir := filepath.Join(l.Root, attemptID)
	if err := os.MkdirAll(l.Root, 0o700); err != nil {
		return "", err
	}
	if err := os.Mkdir(dir, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return "", err
	}
	out := filepath.Join(dir, "out")
	if err := os.Mkdir(out, 0o777); err != nil && !errors.Is(err, os.ErrExist) {
		return "", err
	}
	// Mkdir is subject to the umask; set the mode explicitly so the
	// container's unprivileged user (65534) can write.
	if err := os.Chmod(out, 0o777); err != nil {
		return "", err
	}
	return out, nil
}

func (l LocalDirs) Remove(attemptID string) error {
	return os.RemoveAll(filepath.Join(l.Root, attemptID))
}

// NewLocalDocker returns the Linux Docker runtime rooted at workRoot.
func NewLocalDocker(workRoot, profile string) *Docker {
	return &Docker{
		Cmd:      []string{"docker"},
		WorkRoot: workRoot,
		Mounts:   LocalDirs{Root: filepath.Join(workRoot, "mounts")},
		Profile:  profile,
	}
}

func (d *Docker) Name() string { return "docker-cli" }

func (d *Docker) command(ctx context.Context, args ...string) *exec.Cmd {
	full := append(append([]string{}, d.Cmd[1:]...), args...)
	cmd := exec.CommandContext(ctx, d.Cmd[0], full...)
	cmd.Env = append(os.Environ(), "DOCKER_CLI_HINTS=false")
	return cmd
}

func (d *Docker) output(ctx context.Context, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := d.command(ctx, args...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if len(msg) > 500 {
			msg = msg[:500]
		}
		return stdout.String(), fmt.Errorf("docker %s: %w: %s", args[0], err, msg)
	}
	return stdout.String(), nil
}

// Detect reports tiers: T1 when the daemon answers, T2 when a runsc
// (gVisor) runtime is registered. GPU tiers are decided by the caller
// (needs the NVIDIA Container Toolkit with CDI; untested here).
func (d *Docker) Detect(ctx context.Context) (Info, error) {
	var info Info
	out, err := d.output(ctx, "info", "--format", "{{json .}}")
	if err != nil {
		return info, err
	}
	var raw struct {
		ServerVersion   string                     `json:"ServerVersion"`
		Runtimes        map[string]json.RawMessage `json:"Runtimes"`
		SecurityOptions []string                   `json:"SecurityOptions"`
	}
	if err := json.Unmarshal([]byte(out), &raw); err != nil {
		return info, fmt.Errorf("docker info: %w", err)
	}
	info.ServerVersion = raw.ServerVersion
	info.Tiers = []protocol.Tier{protocol.T1}
	if _, ok := raw.Runtimes["runsc"]; ok {
		info.Runsc = true
		info.Tiers = append(info.Tiers, protocol.T2)
	}
	for _, opt := range raw.SecurityOptions {
		if strings.Contains(opt, "rootless") {
			info.Rootless = true
		}
	}
	info.GPUTiers = []protocol.Tier{}
	return info, nil
}

// Run creates, starts and waits for the attempt's container, then collects
// outputs and removes the container.
func (d *Docker) Run(ctx context.Context, s Spec, onStart func(), logs LogSink) (Result, error) {
	var res Result
	name := ContainerName(s.AttemptID)
	if s.Profile == "" {
		s.Profile = d.Profile
	}
	outMount, err := d.Mounts.Prepare(s.AttemptID)
	if err != nil {
		return res, fmt.Errorf("prepare outputs dir: %w", err)
	}
	d.remember(s.AttemptID, s.Image, outMount)
	args, err := CreateArgs(s, outMount)
	if err != nil {
		return res, err
	}
	// Removing the container on every exit path also stops it.
	defer func() {
		rmCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		_, _ = d.output(rmCtx, "rm", "-f", "-v", name)
	}()

	if s.RegistryAuth != nil {
		pullCtx, cancelPull := context.WithTimeout(ctx, 30*time.Minute)
		err := d.pullPrivate(pullCtx, s.Image, *s.RegistryAuth)
		cancelPull()
		if err != nil {
			if ctx.Err() != nil {
				return res, ErrKilled
			}
			return res, fmt.Errorf("pull private image: %w", err)
		}
	}
	// docker create pulls the image if needed; allow time for large images.
	createCtx, cancelCreate := context.WithTimeout(ctx, 30*time.Minute)
	_, err = d.output(createCtx, args...)
	cancelCreate()
	if err != nil {
		if ctx.Err() != nil {
			return res, ErrKilled
		}
		return res, err
	}
	if _, err := d.output(ctx, "start", name); err != nil {
		if ctx.Err() != nil {
			return res, ErrKilled
		}
		return res, err
	}
	if onStart != nil {
		onStart()
	}

	// Stream logs until the container stops.
	logsDone := make(chan struct{})
	logCmd := d.command(context.Background(), "logs", "--follow", name)
	stdout, _ := logCmd.StdoutPipe()
	stderr, _ := logCmd.StderrPipe()
	if err := logCmd.Start(); err != nil {
		close(logsDone)
	} else {
		var wg sync.WaitGroup
		pump := func(stream string, r io.Reader) {
			defer wg.Done()
			br := bufio.NewReaderSize(r, 32*1024)
			buf := make([]byte, 16*1024)
			for {
				n, err := br.Read(buf)
				if n > 0 && logs != nil {
					chunk := make([]byte, n)
					copy(chunk, buf[:n])
					logs(stream, chunk)
				}
				if err != nil {
					return
				}
			}
		}
		wg.Add(2)
		go pump("stdout", stdout)
		go pump("stderr", stderr)
		go func() {
			wg.Wait()
			_ = logCmd.Wait()
			close(logsDone)
		}()
	}

	type waitResult struct {
		code int
		err  error
	}
	waitCh := make(chan waitResult, 1)
	go func() {
		out, err := d.output(context.Background(), "wait", name)
		if err != nil {
			waitCh <- waitResult{err: err}
			return
		}
		code, perr := strconv.Atoi(strings.TrimSpace(out))
		if perr != nil {
			waitCh <- waitResult{err: fmt.Errorf("docker wait: bad exit code %q", out)}
			return
		}
		waitCh <- waitResult{code: code}
	}()

	var timeout <-chan time.Time
	if s.MaxRuntime > 0 {
		t := time.NewTimer(s.MaxRuntime)
		defer t.Stop()
		timeout = t.C
	}
	var wr waitResult
	select {
	case wr = <-waitCh:
	case <-timeout:
		res.TimedOut = true
		stopCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		_, _ = d.output(stopCtx, "stop", "-t", "10", name)
		cancel()
		wr = <-waitCh
	case <-ctx.Done():
		killCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		_, _ = d.output(killCtx, "rm", "-f", "-v", name)
		cancel()
		<-waitCh
		return res, ErrKilled
	}
	select {
	case <-logsDone:
	case <-time.After(10 * time.Second):
		if logCmd.Process != nil {
			_ = logCmd.Process.Kill()
		}
	}
	// A container removed by Kill/KillAll while running is not a result.
	if ctx.Err() != nil || d.isGone(name) {
		return res, ErrKilled
	}
	if wr.err != nil {
		return res, wr.err
	}
	res.ExitCode = wr.code

	// Collect outputs through the daemon, which can read files whatever
	// their owner or mode inside the container.
	collected := filepath.Join(d.WorkRoot, "collected", s.AttemptID)
	_ = os.RemoveAll(collected)
	cpCtx, cancelCp := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancelCp()
	outPath := s.OutputsPath
	if outPath == "" {
		outPath = protocol.DefaultOutputsPath
	}
	cp := d.command(cpCtx, "cp", name+":"+outPath, "-")
	pipe, _ := cp.StdoutPipe()
	var cpErr bytes.Buffer
	cp.Stderr = &cpErr
	if err := cp.Start(); err != nil {
		res.Notes = append(res.Notes, "could not collect outputs: "+err.Error())
		return res, nil
	}
	maxBytes := int64(0)
	if s.DiskMB > 0 {
		maxBytes = s.DiskMB * 1024 * 1024
	}
	outs, notes, cerr := collectTar(pipe, collected, maxBytes)
	_, _ = io.Copy(io.Discard, pipe)
	werr := cp.Wait()
	res.Outputs, res.Notes = outs, append(res.Notes, notes...)
	if cerr != nil {
		res.Notes = append(res.Notes, "collecting outputs: "+cerr.Error())
	} else if werr != nil {
		res.Notes = append(res.Notes, "docker cp: "+strings.TrimSpace(cpErr.String()))
	}
	return res, nil
}

func (d *Docker) isGone(name string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := d.output(ctx, "inspect", "--format", "{{.Id}}", name)
	return err != nil
}

func (d *Docker) remember(attemptID, image, outDir string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.images == nil {
		d.images, d.outDirs = map[string]string{}, map[string]string{}
	}
	d.images[attemptID] = image
	d.outDirs[attemptID] = outDir
}

func (d *Docker) Stop(ctx context.Context, attemptID string, grace time.Duration) error {
	secs := int(grace / time.Second)
	if secs < 1 {
		secs = 1
	}
	_, err := d.output(ctx, "stop", "-t", strconv.Itoa(secs), ContainerName(attemptID))
	return err
}

func (d *Docker) Kill(ctx context.Context, attemptID string) error {
	_, err := d.output(ctx, "rm", "-f", "-v", ContainerName(attemptID))
	return err
}

func (d *Docker) Pause(ctx context.Context, attemptID string) error {
	_, err := d.output(ctx, "pause", ContainerName(attemptID))
	return err
}

func (d *Docker) Unpause(ctx context.Context, attemptID string) error {
	_, err := d.output(ctx, "unpause", ContainerName(attemptID))
	return err
}

func (d *Docker) Throttle(ctx context.Context, attemptID string, cpus float64) error {
	if cpus < 0.1 {
		cpus = 0.1
	}
	_, err := d.output(ctx, "update", "--cpus", strconv.FormatFloat(cpus, 'f', 2, 64), ContainerName(attemptID))
	return err
}

// KillAll removes every container labelled by this agent.
func (d *Docker) KillAll(ctx context.Context, profile string) (int, error) {
	args := []string{"ps", "-aq", "--filter", "label=" + LabelAgent + "=1"}
	if profile != "" {
		args = append(args, "--filter", "label="+LabelProfile+"="+profile)
	}
	out, err := d.output(ctx, args...)
	if err != nil {
		return 0, err
	}
	ids := strings.Fields(out)
	if len(ids) == 0 {
		return 0, nil
	}
	_, err = d.output(ctx, append([]string{"rm", "-f", "-v"}, ids...)...)
	return len(ids), err
}

// Cleanup removes the attempt's collected outputs and its mount directory.
// Files the container's user created may not be removable by the agent's
// user; then a short-lived container running as that same unprivileged user
// (65534) and the same pinned image deletes them.
func (d *Docker) Cleanup(attemptID string) error {
	_ = os.RemoveAll(filepath.Join(d.WorkRoot, "collected", attemptID))
	err := d.Mounts.Remove(attemptID)
	if err == nil {
		d.forget(attemptID)
		return nil
	}
	d.mu.Lock()
	image, outDir := d.images[attemptID], d.outDirs[attemptID]
	d.mu.Unlock()
	if image == "" || outDir == "" {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	_, rmErr := d.output(ctx, "run", "--rm", "--user", "65534:65534", "--cap-drop", "ALL",
		"--security-opt", "no-new-privileges", "--network", "none", "--read-only",
		"--label", LabelAgent+"=1", "--label", LabelProfile+"="+d.Profile,
		"--mount", "type=bind,src="+outDir+",dst=/out",
		"--entrypoint", "rm", image, "-rf", "--", "/out/")
	// rm -rf /out/ removes the contents but not the mount point itself.
	if err2 := d.Mounts.Remove(attemptID); err2 != nil {
		if rmErr != nil {
			return fmt.Errorf("cleanup %s: %v (cleanup container: %v)", attemptID, err2, rmErr)
		}
		return fmt.Errorf("cleanup %s: %w", attemptID, err2)
	}
	d.forget(attemptID)
	return nil
}

func (d *Docker) forget(attemptID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.images, attemptID)
	delete(d.outDirs, attemptID)
}

// pullPrivate pulls an image with a short-lived token through a throwaway
// Docker config: the token is in a 0600 file inside a 0700 directory that is
// removed right after, never on a command line, and never in the owner's own
// Docker config. The following "docker create" then finds the image locally.
func (d *Docker) pullPrivate(ctx context.Context, image string, auth RegistryAuth) error {
	return d.pullWithConfig(ctx, image, map[string]any{auth.Key: map[string]string{"registrytoken": auth.Token}})
}

// pullWithConfig pulls with exactly these Docker "auths" (none: anonymously),
// ignoring the owner's own Docker login.
func (d *Docker) pullWithConfig(ctx context.Context, image string, auths map[string]any) error {
	if len(d.Cmd) != 1 {
		return errors.New("private images are not supported on this runtime yet (Windows/WSL2)")
	}
	if err := os.MkdirAll(d.WorkRoot, 0o700); err != nil {
		return err
	}
	dir, err := os.MkdirTemp(d.WorkRoot, "pull-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	cfg, err := json.Marshal(map[string]any{"auths": auths})
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "config.json"), cfg, 0o600); err != nil {
		return err
	}
	cmd := d.command(ctx, "--config", dir, "pull", "--quiet", image)
	// A config folder of its own has no Docker contexts: keep talking to the daemon the owner's Docker
	// uses now (Docker Desktop, colima, rootless), unless DOCKER_HOST already says which.
	if os.Getenv("DOCKER_HOST") == "" {
		if host := d.contextHost(ctx); host != "" {
			cmd.Env = append(cmd.Env, "DOCKER_HOST="+host)
		}
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if len(msg) > 500 {
			msg = msg[:500]
		}
		return fmt.Errorf("docker pull: %w: %s", err, msg)
	}
	return nil
}

// contextHost is the daemon address of the current Docker context ("" if Docker cannot say).
func (d *Docker) contextHost(ctx context.Context) string {
	out, err := d.output(ctx, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}
