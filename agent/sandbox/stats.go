package sandbox

// Measuring a run (docs/design/pool-decides.md §4.2). `docker stats` streams one JSON object about once a
// second for a running container: the numbers a person sees with `docker stats`. The agent keeps the highest
// memory use, adds up CPU time, and sends both with the attempt's result. pool learns task sizes from them.

import (
	"bufio"
	"encoding/json"
	"io"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"pool/agent/protocol"
)

// statsSample is one `docker stats` reading.
type statsSample struct {
	CPUPercent float64 // 100 = one core busy for the whole interval
	MemBytes   int64   // memory in use (Docker leaves out reclaimable page cache)
}

// parseStatsLine reads one line of `docker stats --format '{{json .}}'`. The stream wraps each object in
// terminal control codes (ESC[H before it, ESC[K after it); everything outside the braces is ignored.
// It reports false for lines without a usable reading (e.g. "--" while the container starts or stops).
func parseStatsLine(line string) (statsSample, bool) {
	i, j := strings.IndexByte(line, '{'), strings.LastIndexByte(line, '}')
	if i < 0 || j <= i {
		return statsSample{}, false
	}
	var raw struct {
		CPUPerc  string `json:"CPUPerc"`
		MemUsage string `json:"MemUsage"`
	}
	if err := json.Unmarshal([]byte(line[i:j+1]), &raw); err != nil {
		return statsSample{}, false
	}
	cpu, err := strconv.ParseFloat(strings.TrimSuffix(strings.TrimSpace(raw.CPUPerc), "%"), 64)
	if err != nil || cpu < 0 || math.IsNaN(cpu) || math.IsInf(cpu, 0) {
		return statsSample{}, false
	}
	used, _, _ := strings.Cut(raw.MemUsage, "/")
	mem, ok := parseDockerSize(strings.TrimSpace(used))
	if !ok {
		return statsSample{}, false
	}
	return statsSample{CPUPercent: cpu, MemBytes: mem}, true
}

// dockerUnits are the suffixes `docker stats` prints: binary units for memory (1.34MiB), decimal ones for
// I/O (12.3kB). Longer suffixes come first so "MiB" is not read as "B".
var dockerUnits = []struct {
	suffix string
	mult   float64
}{
	{"KiB", 1 << 10}, {"MiB", 1 << 20}, {"GiB", 1 << 30}, {"TiB", 1 << 40},
	{"kB", 1e3}, {"KB", 1e3}, {"MB", 1e6}, {"GB", 1e9}, {"TB", 1e12},
	{"B", 1},
}

func parseDockerSize(s string) (int64, bool) {
	for _, u := range dockerUnits {
		if num, ok := strings.CutSuffix(s, u.suffix); ok {
			v, err := strconv.ParseFloat(strings.TrimSpace(num), 64)
			if err != nil || v < 0 || math.IsNaN(v) || math.IsInf(v, 0) || v*u.mult > math.MaxInt64/2 {
				return 0, false
			}
			return int64(v * u.mult), true
		}
	}
	return 0, false
}

// usageMeter adds up samples. Safe for concurrent use.
type usageMeter struct {
	mu      sync.Mutex
	peakMem int64
	cpuSec  float64
	peakCPU float64
	covered time.Duration // time the CPU readings cover
	samples int
	last    time.Time
}

// maxSampleGap is the longest gap between two readings that still counts for CPU time. A longer gap (a
// paused container, a stalled daemon) is skipped instead of being filled with a guess.
const maxSampleGap = 30 * time.Second

func (m *usageMeter) add(s statsSample, at time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s.MemBytes > m.peakMem {
		m.peakMem = s.MemBytes
	}
	cores := s.CPUPercent / 100
	if cores > m.peakCPU {
		m.peakCPU = cores
	}
	// Docker's CPU percentage covers the time since its previous reading.
	if !m.last.IsZero() {
		if gap := at.Sub(m.last); gap > 0 && gap <= maxSampleGap {
			m.cpuSec += cores * gap.Seconds()
			m.covered += gap
		}
	}
	m.last = at
	m.samples++
}

// read consumes a `docker stats` stream until it ends.
func (m *usageMeter) read(r io.Reader, now func() time.Time) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 4096), 64*1024)
	for sc.Scan() {
		if s, ok := parseStatsLine(sc.Text()); ok {
			m.add(s, now())
		}
	}
}

// usage returns what was measured, rounded for the wire. `docker stats` needs a second or two before its
// first reading, and the run may end between two readings, so the readings cover only part of the run. CPU
// time is the average over the covered part times the whole run time.
func (m *usageMeter) usage(wall time.Duration) *protocol.RunUsage {
	m.mu.Lock()
	defer m.mu.Unlock()
	cpuSec := 0.0
	if m.covered > 0 {
		cpuSec = m.cpuSec / m.covered.Seconds() * wall.Seconds()
	}
	return &protocol.RunUsage{
		PeakMemoryMB: (m.peakMem + (1<<20 - 1)) >> 20,
		CPUSeconds:   math.Round(cpuSec*100) / 100,
		PeakCPU:      math.Round(m.peakCPU*100) / 100,
		WallSeconds:  math.Round(wall.Seconds()*100) / 100,
		Samples:      m.samples,
	}
}
