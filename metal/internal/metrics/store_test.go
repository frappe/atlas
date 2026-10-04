package metrics

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func createVMDirectory(t *testing.T, directory, identifier string) string {
	t.Helper()
	path := filepath.Join(directory, identifier)
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestRetentionAndRestart(t *testing.T) {
	ctx := context.Background()
	directory := t.TempDir()
	vmDirectory := createVMDirectory(t, directory, "one")
	expiredDirectory := createVMDirectory(t, directory, "expired")
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 9, 30, 12, 30, 0, 0, time.UTC)
	cutoff := now.Add(-Retention)
	for _, offset := range []time.Duration{-time.Hour, -time.Second, 0, time.Second, 23 * time.Hour} {
		if err := store.Append("one", Record{Timestamp: cutoff.Add(offset), Metrics: Sample{ComputeUsage: ComputeUsage{MemoryBytes: 42}}}); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Append("expired", Record{Timestamp: cutoff.Add(-25 * time.Hour)}); err != nil {
		t.Fatal(err)
	}
	if err := store.Prune(ctx, now); err != nil {
		t.Fatal(err)
	}
	reopened, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	records, err := reopened.History(ctx, "one", time.Time{}, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 3 || !records[0].Timestamp.Equal(cutoff) || records[0].Metrics.MemoryBytes != 42 {
		t.Fatalf("records=%+v", records)
	}
	if _, err := os.Stat(filepath.Join(vmDirectory, metricsDirectory, cutoff.Format(dayLayout)+".jsonl")); err != nil {
		t.Fatalf("VM metrics day: %v", err)
	}
	expiredFiles, err := os.ReadDir(filepath.Join(expiredDirectory, metricsDirectory))
	if err != nil || len(expiredFiles) != 0 {
		t.Fatalf("expired metrics files: %v, %v", expiredFiles, err)
	}
	if _, err := os.Stat(expiredDirectory); err != nil {
		t.Fatalf("VM directory removed with metrics: %v", err)
	}
	records, err = reopened.History(ctx, "one", cutoff, cutoff.Add(time.Second))
	if err != nil || len(records) != 1 {
		t.Fatalf("range=%+v, %v", records, err)
	}
	records, err = reopened.History(ctx, "other", time.Time{}, now)
	if err != nil || len(records) != 0 {
		t.Fatalf("resource isolation: %+v, %v", records, err)
	}
}

func TestAppendDoesNotRecreateDeletedVM(t *testing.T) {
	directory := t.TempDir()
	vmDirectory := createVMDirectory(t, directory, "one")
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(vmDirectory); err != nil {
		t.Fatal(err)
	}
	if err := store.Append("one", Record{Timestamp: time.Now()}); !os.IsNotExist(err) {
		t.Fatalf("append to deleted VM: %v", err)
	}
	if _, err := os.Stat(vmDirectory); !os.IsNotExist(err) {
		t.Fatalf("deleted VM directory was recreated: %v", err)
	}
}

func TestMetricJSONUsesSnakeCaseAndUnixSeconds(t *testing.T) {
	directory := t.TempDir()
	vmDirectory := createVMDirectory(t, directory, "one")
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 4, 17, 30, 0, 0, time.FixedZone("IST", 5*60*60+30*60))
	record := Record{Timestamp: now, Metrics: Sample{
		Up:           true,
		ComputeUsage: ComputeUsage{CPUUsageMicroseconds: 42, MemoryBytes: 512},
		DiskUsage:    DiskUsage{DiskMiB: 1024, DiskUsedMiB: 128},
		NetworkUsage: NetworkUsage{ReceivedBytes: 1, ReceivedPackets: 2, SentBytes: 3, SentPackets: 4},
	}}
	if err := store.Append("one", record); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(vmDirectory, metricsDirectory, now.UTC().Format(dayLayout)+".jsonl")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var stored struct {
		Timestamp int64                      `json:"timestamp"`
		Metrics   map[string]json.RawMessage `json:"metrics"`
	}
	if err := json.Unmarshal(data, &stored); err != nil {
		t.Fatal(err)
	}
	if stored.Timestamp != now.Unix() || stored.Metrics["cpu_usage_microseconds"] == nil ||
		stored.Metrics["sent_tcp_syn_packets"] == nil || stored.Metrics["disk_read_milli_iops"] == nil ||
		stored.Metrics["CPUUsageMicroseconds"] != nil {
		t.Fatalf("stored metrics = %s", data)
	}
	records, err := store.History(context.Background(), "one", now.Add(-time.Second), now.Add(time.Second))
	if err != nil || len(records) != 1 || !records[0].Timestamp.Equal(now.UTC()) || records[0].Metrics.MemoryBytes != 512 {
		t.Fatalf("stored metrics = %+v, %v", records, err)
	}
}

func TestDownsampledHistoryKeepsLatestCounterInEachBucket(t *testing.T) {
	directory := t.TempDir()
	createVMDirectory(t, directory, "one")
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Date(2026, 10, 4, 12, 0, 0, 0, time.UTC)
	for _, second := range []int{10, 20, 310} {
		at := start.Add(time.Duration(second) * time.Second)
		if err := store.Append("one", Record{Timestamp: at, Metrics: Sample{
			DiskUsage:    DiskUsage{DiskReadBytesPerSecond: uint64(second)},
			NetworkUsage: NetworkUsage{SentPackets: uint64(second)},
		}}); err != nil {
			t.Fatal(err)
		}
	}
	records, err := store.DownsampledHistory(t.Context(), "one", start, start.Add(10*time.Minute), 5*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 2 || records[0].Metrics.SentPackets != 20 || records[0].Metrics.DiskReadBytesPerSecond != 15 ||
		records[1].Metrics.SentPackets != 310 || records[1].Metrics.DiskReadBytesPerSecond != 310 {
		t.Fatalf("downsampled records = %+v", records)
	}
}
