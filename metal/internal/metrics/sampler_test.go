package metrics

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
)

type sampleSources struct {
	hostError error
	listError error
}

func (source sampleSources) Capacity(context.Context) (host.Capacity, error) {
	return host.Capacity{TotalMemoryMiB: 1024}, source.hostError
}

func (source sampleSources) ListIDs(context.Context) ([]string, error) {
	return []string{"failed", "running", "stopped"}, source.listError
}

func (source sampleSources) Metrics(_ context.Context, id string) (vm.Metrics, error) {
	if id == "failed" {
		return vm.Metrics{}, errors.New("unavailable")
	}
	return vm.Metrics{Up: id == "running", MemoryBytes: 42}, nil
}

func TestSamplingIsolatesFailuresAndRetainsStoppedVMs(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	source := sampleSources{hostError: errors.New("unavailable")}
	sampler := NewSampler(store, source, source, slog.New(slog.NewTextHandler(io.Discard, nil)))
	sampler.Collect(t.Context())
	for _, id := range []string{"running", "stopped"} {
		records, err := store.History(t.Context(), "vm:"+id, time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
		if err != nil || len(records) != 1 || records[0].VM.Up != (id == "running") {
			t.Fatalf("%s: %+v, %v", id, records, err)
		}
	}
	records, err := store.History(t.Context(), "vm:failed", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	if err != nil || len(records) != 0 {
		t.Fatalf("failed sample fabricated: %+v, %v", records, err)
	}
	source = sampleSources{listError: errors.New("unavailable")}
	sampler = NewSampler(store, source, source, slog.New(slog.NewTextHandler(io.Discard, nil)))
	sampler.Collect(t.Context())
	records, err = store.History(t.Context(), "host", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	if err != nil || len(records) != 1 || records[0].Host.TotalMemoryMiB != 1024 {
		t.Fatalf("host: %+v, %v", records, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	done := make(chan struct{})
	go func() { sampler.Run(ctx); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("worker did not stop")
	}
}
