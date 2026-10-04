package metrics

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
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
