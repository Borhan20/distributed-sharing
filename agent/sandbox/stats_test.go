package sandbox

import (
	"strings"
	"testing"
	"time"
)

func TestParseStatsLine(t *testing.T) {
	// A real frame from `docker stats --format '{{json .}}'`, with its terminal control codes.
	line := "\x1b[H{\"BlockIO\":\"0B / 0B\",\"CPUPerc\":\"101.10%\",\"Container\":\"x\",\"MemPerc\":\"0.33%\"," +
		"\"MemUsage\":\"876KiB / 256MiB\",\"Name\":\"x\",\"NetIO\":\"0B / 0B\",\"PIDs\":\"1\"}\x1b[K"
	s, ok := parseStatsLine(line)
	if !ok || s.CPUPercent != 101.10 || s.MemBytes != 876*1024 {
		t.Fatalf("got %+v ok=%v", s, ok)
	}
	for _, bad := range []string{
		"", "\x1b[K", "{not json}", `{"CPUPerc":"--","MemUsage":"-- / --"}`, `{"CPUPerc":"-5%","MemUsage":"1MiB / 2MiB"}`,
		`{"CPUPerc":"NaN%","MemUsage":"1MiB / 2MiB"}`, `{"CPUPerc":"1%","MemUsage":"lots / 2MiB"}`,
		`{"CPUPerc":"1%","MemUsage":"99999999999TiB / 2MiB"}`,
	} {
		if _, ok := parseStatsLine(bad); ok {
			t.Errorf("accepted %q", bad)
		}
	}
}

func TestParseDockerSize(t *testing.T) {
	cases := map[string]int64{
		"0B": 0, "512B": 512, "1.5KiB": 1536, "1.34MiB": 1405091, "2GiB": 2 << 30, "12.3kB": 12300, "1MB": 1000000,
	}
	for in, want := range cases {
		if got, ok := parseDockerSize(in); !ok || got != want {
			t.Errorf("%s: got %d ok=%v, want %d", in, got, ok, want)
		}
	}
}

func TestUsageMeterAddsCPUTimeAndKeepsPeaks(t *testing.T) {
	m := &usageMeter{}
	t0 := time.Unix(1000, 0)
	m.add(statsSample{CPUPercent: 0, MemBytes: 10 << 20}, t0)                      // first reading: no interval yet
	m.add(statsSample{CPUPercent: 200, MemBytes: 300 << 20}, t0.Add(time.Second))  // 2 cores for 1 s
	m.add(statsSample{CPUPercent: 50, MemBytes: 100 << 20}, t0.Add(3*time.Second)) // 0.5 cores for 2 s
	m.add(statsSample{CPUPercent: 100, MemBytes: 1}, t0.Add(time.Hour))            // a gap: not counted
	// 3 CPU seconds over the 3 s the readings cover: 1 core on average, so 90 s of run time is 90 CPU seconds.
	u := m.usage(90 * time.Second)
	if u.CPUSeconds != 90 || u.PeakCPU != 2 || u.PeakMemoryMB != 300 || u.Samples != 4 || u.WallSeconds != 90 {
		t.Fatalf("got %+v", u)
	}
}

func TestUsageMeterReadsAStream(t *testing.T) {
	frame := func(cpu, mem string) string {
		return "\x1b[H{\"CPUPerc\":\"" + cpu + "\",\"MemUsage\":\"" + mem + " / 1GiB\"}\x1b[K\n\x1b[K\n\x1b[J"
	}
	// Before the start and after the end, Docker reads "0B / 0.00%": those frames are not the program.
	stream := frame("0.00%", "0B") + frame("0.00%", "1MiB") + frame("100.00%", "64MiB") + frame("0.00%", "0B") +
		frame("--", "--")
	clock := time.Unix(0, 0)
	m := &usageMeter{}
	m.read(strings.NewReader(stream), func() time.Time { clock = clock.Add(time.Second); return clock })
	u := m.usage(2 * time.Second)
	if u.Samples != 2 || u.PeakMemoryMB != 64 || u.CPUSeconds != 2 {
		t.Fatalf("got %+v", u)
	}
}
