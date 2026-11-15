package metrics

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type parallelSource struct {
	started chan struct{}
	release chan struct{}
	calls   atomic.Int32
}

func (source *parallelSource) ListIDs(context.Context) ([]string, error) {
	return []string{"one", "two"}, nil
}

func (source *parallelSource) RecordMetrics(context.Context, string, Record) error { return nil }

func (source *parallelSource) Metrics(ctx context.Context, _ string) (Sample, error) {
	if source.calls.Add(1) == 2 {
		close(source.started)
	}
	select {
	case <-source.release:
		return Sample{}, nil
	case <-ctx.Done():
		return Sample{}, ctx.Err()
	}
}

func TestCollectReadsVMsConcurrently(t *testing.T) {
	directory := t.TempDir()
	for _, id := range []string{"one", "two"} {
		if err := os.Mkdir(filepath.Join(directory, id), 0700); err != nil {
			t.Fatal(err)
		}
	}
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	source := &parallelSource{started: make(chan struct{}), release: make(chan struct{})}
	sampler := NewSampler(store, source, slog.New(slog.NewTextHandler(io.Discard, nil)))
	done := make(chan struct{})
	go func() {
		sampler.Collect(t.Context())
		close(done)
	}()
	select {
	case <-source.started:
	case <-time.After(2 * time.Second):
		close(source.release)
		<-done
		t.Fatal("VM collection ran sequentially")
	}
	close(source.release)
	<-done
}

func TestDiskRatesStartAndResetAtZero(t *testing.T) {
	start := time.Unix(100, 0)
	sampler := &Sampler{previousDisk: map[string]diskSnapshot{}}
	first := Sample{Up: true, DiskUsage: DiskUsage{Counters: DiskCounters{ReadBytes: 100, ReadOperations: 2}}}
	if got := sampler.withDiskRates("vm", start, first); got.DiskReadBytesPerSecond != 0 {
		t.Fatalf("first disk rate = %v", got.DiskReadBytesPerSecond)
	}
	sampler.previousDisk["vm"] = diskSnapshot{counters: first.Counters, time: start}
	second := Sample{Up: true, DiskUsage: DiskUsage{Counters: DiskCounters{ReadBytes: 200, ReadOperations: 5}}}
	got := sampler.withDiskRates("vm", start.Add(10*time.Second), second)
	if got.DiskReadBytesPerSecond != 10 || got.DiskReadMilliIOPS != 300 {
		t.Fatalf("disk rates = %+v", got.DiskUsage)
	}
	reset := Sample{Up: true, DiskUsage: DiskUsage{Counters: DiskCounters{ReadBytes: 1}}}
	if got := sampler.withDiskRates("vm", start.Add(20*time.Second), reset); got.DiskReadBytesPerSecond != 0 || got.DiskReadMilliIOPS != 0 {
		t.Fatalf("reset disk rates = %+v", got.DiskUsage)
	}
}

// recordingSource returns one fixed sample for each VM and keeps every record
// passed to RecordMetrics.
type recordingSource struct {
	identifiers []string
	mutex       sync.Mutex
	recorded    map[string]Record
}

func (source *recordingSource) ListIDs(context.Context) ([]string, error) {
	return source.identifiers, nil
}

func (source *recordingSource) Metrics(context.Context, string) (Sample, error) {
	return Sample{Up: true, ComputeUsage: ComputeUsage{CPUTimeMicroseconds: 42}}, nil
}

func (source *recordingSource) RecordMetrics(_ context.Context, identifier string, record Record) error {
	source.mutex.Lock()
	defer source.mutex.Unlock()
	source.recorded[identifier] = record
	return nil
}

// collectFrom runs one sweep over identifiers in a store where only the
// existing VMs have a records directory.
func collectFrom(t *testing.T, identifiers, existing []string) (*Store, *recordingSource) {
	t.Helper()
	directory := t.TempDir()
	for _, identifier := range existing {
		if err := os.Mkdir(filepath.Join(directory, identifier), 0700); err != nil {
			t.Fatal(err)
		}
	}
	store, err := NewStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	source := &recordingSource{identifiers: identifiers, recorded: map[string]Record{}}
	NewSampler(store, source, slog.New(slog.NewTextHandler(io.Discard, nil))).Collect(t.Context())
	return store, source
}

// TestCollectRecordsStoredMetrics checks that RecordMetrics receives the record
// that the store kept.
func TestCollectRecordsStoredMetrics(t *testing.T) {
	store, source := collectFrom(t, []string{"one"}, []string{"one"})

	stored, err := store.History(t.Context(), "one", time.Now().Add(-time.Minute), time.Now().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	recorded, found := source.recorded["one"]
	if len(stored) != 1 || !found {
		t.Fatalf("stored = %d, recorded = %t, want 1 and true", len(stored), found)
	}
	if recorded.Timestamp.Unix() != stored[0].Timestamp.Unix() || recorded.Metrics != stored[0].Metrics {
		t.Fatalf("recorded = %+v, stored = %+v", recorded, stored[0])
	}
}

// TestCollectSkipsRecordWhenAppendFails checks that a VM whose records
// directory is gone, so the store rejects its sample, never reaches RecordMetrics.
func TestCollectSkipsRecordWhenAppendFails(t *testing.T) {
	_, source := collectFrom(t, []string{"removed"}, nil)

	if len(source.recorded) != 0 {
		t.Fatalf("recorded = %v, want none", source.recorded)
	}
}
