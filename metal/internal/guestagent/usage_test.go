package guestagent

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadUsageParsesProcFiles(t *testing.T) {
	procRoot := t.TempDir()
	writeFile(t, filepath.Join(procRoot, "loadavg"), "0.42 0.31 0.25 2/187 4242\n")
	writeFile(t, filepath.Join(procRoot, "meminfo"), "MemTotal:        2048 kB\nMemFree:          512 kB\nMemAvailable:    1024 kB\nBuffers:           64 kB\nCached:           256 kB\nSwapCached:         8 kB\nSwapTotal:          0 kB\nSwapFree:           0 kB\n")

	usage, err := ReadUsage(procRoot)
	if err != nil {
		t.Fatal(err)
	}

	if usage.LoadAverage != [3]float64{0.42, 0.31, 0.25} {
		t.Fatalf("load average = %v", usage.LoadAverage)
	}
	want := Memory{TotalBytes: 2048 << 10, FreeBytes: 512 << 10, AvailableBytes: 1024 << 10, CachedBytes: 256 << 10}
	if usage.Memory != want {
		t.Fatalf("memory = %+v, want %+v", usage.Memory, want)
	}
	if usage.RootFilesystem.SizeBytes == 0 || usage.RootFilesystem.UsedBytes > usage.RootFilesystem.SizeBytes {
		t.Fatalf("root filesystem = %+v", usage.RootFilesystem)
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
