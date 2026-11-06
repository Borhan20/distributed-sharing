// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

// Package sandbox runs one attempt in a hardened container (plan §8.3).
package sandbox

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"pool/agent/protocol"
)

// MaxOutputFiles caps the number of output files per attempt (the protocol
// allows at most 1000 outputs in a commit).
const MaxOutputFiles = 1000

// DefaultPidsLimit is the process-count limit for every container.
const DefaultPidsLimit = 512

// Spec describes one attempt to run.
type Spec struct {
	AttemptID   string
	TaskID      string
	Epoch       int64
	Group       string
	Profile     string
	Tier        protocol.Tier
	Image       string
	Command     []string // replaces the image's ENTRYPOINT; empty: keep it
	Args        []string // after Command, or for the image's ENTRYPOINT (like `docker run IMAGE ARGS`); empty: CMD
	Workdir     string   // working directory inside the container; empty: the image's
	Env         map[string]string
	TaskIndex   int // this task's number in its job, 0..TaskCount-1 (POOL_TASK_INDEX)
	TaskCount   int // tasks in the job (POOL_TASK_COUNT)
	CPU         float64
	MemoryMB    int64
	DiskMB      int64
	GPUs        int
	PidsLimit   int
	MaxRuntime  time.Duration
	OutputsPath string // inside the container, default /out
	// Ports an app listens on (pool-run step 2). The container still has no network: the agent reaches a
	// port with `docker exec ... /.pool/pool-agent pipe 127.0.0.1:PORT` (Runtime.Tunnel).
	Ports []int
	// Helper is the static pool-agent binary mounted read-only at HelperPath in an app's container.
	Helper string
	// RegistryAuth, when set, is a short-lived token to pull a private image.
	RegistryAuth *RegistryAuth
}

// RegistryAuth is a bearer token for one image, from the server (pull_auth).
// Key is the registry's key in Docker's config "auths".
type RegistryAuth struct {
	Key   string
	Token string
}

// Output is one collected output file.
type Output struct {
	protocol.ArtifactRef
	LocalPath string // agent-owned copy, used for upload
}

// Result of a finished attempt.
type Result struct {
	ExitCode int
	TimedOut bool
	Outputs  []Output
	Notes    []string           // non-fatal problems (skipped files, cleanup failures)
	Usage    *protocol.RunUsage // what the run used; nil when the runtime cannot measure
}

// LogSink receives container output. stream is "stdout" or "stderr".
type LogSink func(stream string, data []byte)

// ErrKilled is returned by Run when the container was removed while running
// (Kill, KillAll, cancel, stale lease).
var ErrKilled = errors.New("container was killed")

// Runtime runs attempts. Implementations must be safe for concurrent use.
type Runtime interface {
	Name() string
	// Detect reports the tiers this runtime can provide.
	Detect(ctx context.Context) (Info, error)
	// Run blocks until the attempt finishes. onStart is called once the
	// container has started (Assigned -> Running).
	Run(ctx context.Context, spec Spec, onStart func(), logs LogSink) (Result, error)
	// Stop asks the container to stop (SIGTERM, then SIGKILL after grace).
	Stop(ctx context.Context, attemptID string, grace time.Duration) error
	// Kill removes the container immediately.
	Kill(ctx context.Context, attemptID string) error
	Pause(ctx context.Context, attemptID string) error
	Unpause(ctx context.Context, attemptID string) error
	// Throttle changes the CPU limit of a running container.
	Throttle(ctx context.Context, attemptID string, cpus float64) error
	// KillAll removes every container started by this agent (profile "" = all profiles).
	KillAll(ctx context.Context, profile string) (int, error)
	// Cleanup removes the attempt's local files.
	Cleanup(attemptID string) error
	// Tunnel connects to an app's port inside its running container (pool-run step 2).
	Tunnel(ctx context.Context, attemptID string, port int) (io.ReadWriteCloser, error)
}

// HelperPath is where an app's container sees the pool-agent binary (for `pool-agent pipe`).
const HelperPath = "/.pool/pool-agent"

// Info describes what the runtime can do on this device.
type Info struct {
	Tiers         []protocol.Tier
	GPUTiers      []protocol.Tier
	Rootless      bool
	Runsc         bool
	ServerVersion string
	Apps          bool // can run apps: it has a static helper to reach their ports
}

// ContainerName returns the container name for an attempt.
func ContainerName(attemptID string) string { return "pool-" + attemptID }

// Labels used on every container.
const (
	LabelAgent   = "pool.agent"
	LabelAttempt = "pool.attempt"
	LabelGroup   = "pool.group"
	LabelProfile = "pool.profile"
)

// CreateArgs builds the `docker create` arguments for a spec. outMount is the
// path of the attempt's output directory as seen by the Docker daemon.
// Flags follow design doc §8; see sandbox_docker_test.go for the escape tests.
func CreateArgs(s Spec, outMount string) ([]string, error) {
	if s.AttemptID == "" || s.Image == "" {
		return nil, errors.New("spec needs attempt id and image")
	}
	if s.Workdir != "" && (!path.IsAbs(s.Workdir) || strings.ContainsRune(s.Workdir, 0) ||
		strings.Contains("/"+s.Workdir+"/", "/../")) {
		return nil, fmt.Errorf("bad working directory %q", s.Workdir)
	}
	if s.TaskIndex < 0 || (s.TaskCount > 0 && s.TaskIndex >= s.TaskCount) {
		return nil, fmt.Errorf("task index %d is outside 0..%d", s.TaskIndex, s.TaskCount-1)
	}
	if s.CPU <= 0 || s.MemoryMB <= 0 {
		return nil, errors.New("spec needs cpu and memory limits")
	}
	if s.Tier != protocol.T1 && s.Tier != protocol.T2 {
		return nil, fmt.Errorf("tier %q is not supported by the docker runtime", s.Tier)
	}
	outPath := s.OutputsPath
	if outPath == "" {
		outPath = protocol.DefaultOutputsPath
	}
	if !path.IsAbs(outPath) || strings.Contains(outPath, "..") || strings.ContainsAny(outPath, ",=") {
		return nil, fmt.Errorf("bad outputs path %q", outPath)
	}
	if strings.ContainsAny(outMount, ",=") {
		return nil, fmt.Errorf("output mount path %q contains ',' or '='", outMount)
	}
	pids := s.PidsLimit
	if pids <= 0 {
		pids = DefaultPidsLimit
	}
	mem := strconv.FormatInt(s.MemoryMB, 10) + "m"
	args := []string{
		"create",
		"--name", ContainerName(s.AttemptID),
		"--label", LabelAgent + "=1",
		"--label", LabelAttempt + "=" + s.AttemptID,
		"--label", LabelGroup + "=" + s.Group,
		"--label", LabelProfile + "=" + s.Profile,
		"--user", "65534:65534",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges",
		"--read-only",
		"--network", "none",
		"--ipc", "private",
		"--pids-limit", strconv.Itoa(pids),
		"--memory", mem,
		"--memory-swap", mem,
		"--cpus", strconv.FormatFloat(s.CPU, 'f', -1, 64),
		"--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
		"--log-driver", "json-file",
		"--log-opt", "max-size=20m",
		"--log-opt", "max-file=2",
		"--stop-timeout", "30",
		"--mount", "type=bind,src=" + outMount + ",dst=" + outPath,
	}
	if s.Tier == protocol.T2 {
		args = append(args, "--runtime", "runsc")
	}
	if len(s.Ports) > 0 {
		if s.Helper == "" || strings.ContainsAny(s.Helper, ",=") {
			return nil, errors.New("apps need a static pool-agent binary to reach their ports (build it with CGO_ENABLED=0)")
		}
		args = append(args, "--mount", "type=bind,src="+s.Helper+",dst="+HelperPath+",readonly")
	}
	if s.GPUs > 0 {
		// CDI device name from the NVIDIA Container Toolkit. UNTESTED here (no GPU).
		args = append(args, "--device", "nvidia.com/gpu=all")
	}
	env := map[string]string{}
	for k, v := range s.Env {
		if strings.HasPrefix(k, "POOL_") || k == "" || strings.ContainsAny(k, "=\x00") {
			return nil, fmt.Errorf("invalid env name %q", k)
		}
		env[k] = v
	}
	env["POOL_ATTEMPT_ID"] = s.AttemptID
	env["POOL_TASK_ID"] = s.TaskID
	env["POOL_EPOCH"] = strconv.FormatInt(s.Epoch, 10)
	env["POOL_OUTPUT_DIR"] = outPath
	count := s.TaskCount
	if count < 1 {
		count = 1
	}
	env["POOL_TASK_INDEX"] = strconv.Itoa(s.TaskIndex)
	env["POOL_TASK_COUNT"] = strconv.Itoa(count)
	keys := make([]string, 0, len(env))
	for k := range env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if strings.ContainsRune(env[k], 0) {
			return nil, fmt.Errorf("env %s contains a NUL byte", k)
		}
		args = append(args, "--env", k+"="+env[k])
	}
	if s.Workdir != "" {
		args = append(args, "--workdir", s.Workdir)
	}
	if len(s.Command) == 0 {
		// No command: the image's own ENTRYPOINT runs, with Args in place of its CMD when given,
		// as with `docker run IMAGE ARGS`.
		args = append(args, s.Image)
		return append(args, s.Args...), nil
	}
	// The job's command replaces the image ENTRYPOINT (like Kubernetes "command"), then its arguments.
	args = append(args, "--entrypoint", s.Command[0], s.Image)
	args = append(args, s.Command[1:]...)
	return append(args, s.Args...), nil
}

// collectTar reads a `docker cp` tar stream of the outputs directory, copies
// regular files into dest (agent-owned) and hashes them. Symlinks, devices
// and anything escaping dest are skipped.
func collectTar(r io.Reader, dest string, maxBytes int64) ([]Output, []string, error) {
	if err := os.MkdirAll(dest, 0o700); err != nil {
		return nil, nil, err
	}
	tr := tar.NewReader(r)
	var outs []Output
	var notes []string
	var total int64
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return outs, notes, fmt.Errorf("read outputs: %w", err)
		}
		// docker cp prefixes entries with the base name of the copied dir.
		name := h.Name
		if i := strings.IndexByte(name, '/'); i >= 0 {
			name = name[i+1:]
		} else {
			continue // the directory itself
		}
		clean := path.Clean(name)
		if clean == "." || clean == "" || strings.HasPrefix(clean, "../") || path.IsAbs(clean) {
			continue
		}
		if h.Typeflag != tar.TypeReg {
			if h.Typeflag != tar.TypeDir {
				notes = append(notes, fmt.Sprintf("skipped non-regular output %s", clean))
			}
			continue
		}
		if len(outs) >= MaxOutputFiles {
			notes = append(notes, fmt.Sprintf("more than %d output files; the rest were skipped", MaxOutputFiles))
			break
		}
		total += h.Size
		if maxBytes > 0 && total > maxBytes {
			notes = append(notes, fmt.Sprintf("outputs exceed %d bytes; the rest were skipped", maxBytes))
			break
		}
		local := filepath.Join(dest, filepath.FromSlash(clean))
		if !strings.HasPrefix(local, filepath.Clean(dest)+string(os.PathSeparator)) {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(local), 0o700); err != nil {
			return outs, notes, err
		}
		f, err := os.OpenFile(local, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
		if err != nil {
			return outs, notes, err
		}
		hsh := sha256.New()
		n, err := io.Copy(io.MultiWriter(f, hsh), tr)
		cerr := f.Close()
		if err != nil {
			return outs, notes, err
		}
		if cerr != nil {
			return outs, notes, cerr
		}
		outs = append(outs, Output{
			ArtifactRef: protocol.ArtifactRef{Path: clean, SHA256: hex.EncodeToString(hsh.Sum(nil)), Size: n},
			LocalPath:   local,
		})
	}
	sort.Slice(outs, func(i, j int) bool { return outs[i].Path < outs[j].Path })
	return outs, notes, nil
}
