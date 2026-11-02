//go:build !linux && !windows

package sysinfo

func memoryMB() int64         { return 0 }
func freeDiskMB(string) int64 { return 0 }
