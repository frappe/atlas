package metrics

import (
	"context"
	"testing"
	"time"
)

func cpuRecord(timestamp time.Time, up bool, cpuMicroseconds uint64) Record {
	return Record{Timestamp: timestamp, Metrics: Sample{Up: up, ComputeUsage: ComputeUsage{CPUTimeMicroseconds: cpuMicroseconds}}}
}

func TestMeasureCPUUsageSkipsDowntimeAndRestarts(t *testing.T) {
	start := time.Date(2026, 11, 12, 10, 0, 0, 0, time.UTC)
	at := func(seconds int) time.Time { return start.Add(time.Duration(seconds) * time.Second) }
	records := []Record{
		cpuRecord(at(0), true, 0),
		cpuRecord(at(10), true, 5_000_000),
		cpuRecord(at(20), false, 0),
		cpuRecord(at(30), true, 1_000_000),
		cpuRecord(at(40), true, 3_000_000),
	}

	usage := MeasureCPUUsage(records)
	if usage.CPUSeconds != 7 || usage.UpSeconds != 20 {
		t.Fatalf("usage=%+v", usage)
	}
	if percent, ok := usage.Percent(500); !ok || percent != 70 {
		t.Fatalf("percent=%v ok=%v", percent, ok)
	}
}

func TestCPUUsagePercentWithoutUpTime(t *testing.T) {
	usage := MeasureCPUUsage([]Record{cpuRecord(time.Now(), true, 9)})
	if _, ok := usage.Percent(1000); ok {
		t.Fatalf("percent without up time: %+v", usage)
	}
}

func TestSlidingWindowCPUUsageReadsOnlyTheWindow(t *testing.T) {
	directory := t.TempDir()
	createVMDirectory(t, directory, "one")
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	for _, record := range []Record{
		cpuRecord(now.Add(-2*time.Hour), true, 0),
		cpuRecord(now.Add(-30*time.Minute), true, 100_000_000),
		cpuRecord(now.Add(-20*time.Minute), true, 400_000_000),
	} {
		if err := store.Append("one", record); err != nil {
			t.Fatal(err)
		}
	}

	usage, err := store.SlidingWindowCPUUsage(context.Background(), "one", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if usage.CPUSeconds != 300 || usage.UpSeconds != 600 {
		t.Fatalf("usage=%+v", usage)
	}
}
