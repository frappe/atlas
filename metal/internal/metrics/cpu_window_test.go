package metrics

import (
	"testing"
	"time"
)

func TestCPUWindowMeasuresOnlyTheWindow(t *testing.T) {
	window := NewCPUWindow(time.Hour)
	start := time.Date(2026, 11, 13, 10, 0, 0, 0, time.UTC)
	for _, record := range []Record{
		cpuRecord(start, true, 0),
		cpuRecord(start.Add(90*time.Minute), true, 100_000_000),
		cpuRecord(start.Add(100*time.Minute), true, 400_000_000),
	} {
		window.Add("one", record)
	}

	usage := window.Usage("one", start.Add(50*time.Minute))
	if usage.CPUSeconds != 300 || usage.UpSeconds != 600 {
		t.Fatalf("usage=%+v", usage)
	}
	if records := len(window.records["one"]); records != 2 {
		t.Fatalf("kept %d records, want 2", records)
	}
}

func TestCPUWindowRetainsOnlyActiveVirtualMachines(t *testing.T) {
	window := NewCPUWindow(time.Hour)
	now := time.Now()
	window.Add("one", cpuRecord(now, true, 0))
	window.Add("two", cpuRecord(now, true, 0))

	window.Retain(map[string]bool{"one": true})
	if _, ok := window.records["two"]; ok || len(window.records) != 1 {
		t.Fatalf("records=%v", window.records)
	}
}
