package platform

import (
	"path/filepath"
	"testing"
)

func TestParseMemoryCurrent(t *testing.T) {
	value, err := parseMemoryCurrent([]byte("1048576\n"))
	if err != nil {
		t.Fatal(err)
	}
	if value != 1048576 {
		t.Fatalf("value = %d", value)
	}
}

func TestParseMemoryCurrentRejectsGarbage(t *testing.T) {
	if _, err := parseMemoryCurrent([]byte("not-a-number")); err == nil {
		t.Fatal("expected an error")
	}
}

func TestParseCPUUsageMicroseconds(t *testing.T) {
	value, err := parseCPUUsageMicroseconds([]byte("usage_usec 500\nuser_usec 300\nsystem_usec 200\n"))
	if err != nil {
		t.Fatal(err)
	}
	if value != 500 {
		t.Fatalf("value = %d", value)
	}
}

func TestParseCPUUsageMicrosecondsRejectsAMissingField(t *testing.T) {
	if _, err := parseCPUUsageMicroseconds([]byte("user_usec 300\n")); err == nil {
		t.Fatal("expected an error")
	}
}

func TestReadMemoryCurrentToleratesARemovedCgroup(t *testing.T) {
	value, err := readMemoryCurrent(filepath.Join(t.TempDir(), "memory.current"))
	if err != nil {
		t.Fatal(err)
	}
	if value != 0 {
		t.Fatalf("value = %d", value)
	}
}

func TestReadCPUUsageMicrosecondsToleratesARemovedCgroup(t *testing.T) {
	value, err := readCPUUsageMicroseconds(filepath.Join(t.TempDir(), "cpu.stat"))
	if err != nil {
		t.Fatal(err)
	}
	if value != 0 {
		t.Fatalf("value = %d", value)
	}
}

func TestReadUsageToleratesARemovedCgroup(t *testing.T) {
	usage, err := readUsage("does/not/exist")
	if err != nil {
		t.Fatal(err)
	}
	if usage != (Usage{}) {
		t.Fatalf("usage = %+v", usage)
	}
}
