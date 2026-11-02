// Package sysinfo reports CPU, memory, disk and GPUs for capability reports.
package sysinfo

import (
	"context"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	"pool/agent/protocol"
)

// Info is a snapshot of device resources.
type Info struct {
	OS         string
	Arch       string
	CPUCores   int
	MemoryMB   int64
	FreeDiskMB int64
	GPUs       []protocol.GPUInfo
}

// Collect gathers device facts. workDir is where attempt data is stored.
func Collect(workDir string) Info {
	info := Info{OS: runtime.GOOS, Arch: runtime.GOARCH, CPUCores: runtime.NumCPU()}
	info.MemoryMB = memoryMB()
	info.FreeDiskMB = freeDiskMB(workDir)
	info.GPUs = NvidiaGPUs()
	return info
}

// NvidiaGPUs lists NVIDIA GPUs via nvidia-smi (NVML-backed). Returns an
// empty list when nvidia-smi is missing. UNTESTED on real GPUs here.
func NvidiaGPUs() []protocol.GPUInfo {
	out := []protocol.GPUInfo{}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	raw, err := exec.CommandContext(ctx, "nvidia-smi", "--query-gpu=name,memory.total",
		"--format=csv,noheader,nounits").Output()
	if err != nil {
		return out
	}
	return ParseNvidiaSMI(string(raw))
}

// ParseNvidiaSMI parses "name, memory_MiB" lines.
func ParseNvidiaSMI(raw string) []protocol.GPUInfo {
	out := []protocol.GPUInfo{}
	for _, line := range strings.Split(raw, "\n") {
		name, mem, ok := strings.Cut(line, ",")
		if !ok {
			continue
		}
		mb, err := strconv.ParseInt(strings.TrimSpace(mem), 10, 64)
		if err != nil {
			continue
		}
		out = append(out, protocol.GPUInfo{Vendor: "nvidia", Model: strings.TrimSpace(name), MemoryMB: mb})
	}
	return out
}
