package sysinfo

import "testing"

func TestCollect(t *testing.T) {
	info := Collect(t.TempDir())
	if info.CPUCores < 1 || info.OS == "" {
		t.Fatalf("info: %+v", info)
	}
	if info.GPUs == nil {
		t.Fatal("GPUs must be an empty list, not nil")
	}
}

func TestParseNvidiaSMI(t *testing.T) {
	gpus := ParseNvidiaSMI("Example GPU A, 24576\nExample GPU B, 8192\n\nbad line\n")
	if len(gpus) != 2 || gpus[0].MemoryMB != 24576 || gpus[1].Model != "Example GPU B" || gpus[0].Vendor != "nvidia" {
		t.Fatalf("%+v", gpus)
	}
}
