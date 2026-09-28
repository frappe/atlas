package datum

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeVirtualMachines struct {
	ids     []string
	metrics map[string]vm.Metrics
}

func (f *fakeVirtualMachines) ListIDs(context.Context) ([]string, error) { return f.ids, nil }

func (f *fakeVirtualMachines) Metrics(_ context.Context, id string) (vm.Metrics, error) {
	return f.metrics[id], nil
}

type fakeHostCapacity struct {
	capacity host.Capacity
}

func (f *fakeHostCapacity) Capacity(context.Context) (host.Capacity, error) {
	return f.capacity, nil
}

type fakeIngester struct {
	mutex sync.Mutex
	calls []ingestCall
}

type ingestCall struct {
	token   string
	samples []Sample
}

func (f *fakeIngester) Ingest(_ context.Context, token string, samples []Sample) error {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	f.calls = append(f.calls, ingestCall{token: token, samples: samples})
	return nil
}

func writeBundle(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "datum-tokens.json")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestPushAllSendsHostAndEveryTokenedVM(t *testing.T) {
	path := writeBundle(t, `{"host":"host-token","vms":{"vm-1":"vm-1-token"}}`)
	ingester := &fakeIngester{}
	virtualMachines := &fakeVirtualMachines{
		ids:     []string{"vm-1", "vm-2"},
		metrics: map[string]vm.Metrics{"vm-1": {Up: true, MemoryBytes: 1024, CPUUsageMicroseconds: 500}},
	}
	capacity := &fakeHostCapacity{capacity: host.Capacity{TotalCPUMillicores: 4000}}

	exporter := NewExporter(nil, path, virtualMachines, capacity, time.Second, nil)
	exporter.client = ingester

	exporter.PushAll(context.Background())

	if len(ingester.calls) != 2 {
		t.Fatalf("expected 2 pushes (host + vm-1), got %d", len(ingester.calls))
	}
	tokens := map[string]bool{}
	for _, call := range ingester.calls {
		tokens[call.token] = true
	}
	if !tokens["host-token"] || !tokens["vm-1-token"] {
		t.Fatalf("unexpected tokens: %v", tokens)
	}
}

func TestPushAllSkipsAVMWithNoTokenYet(t *testing.T) {
	path := writeBundle(t, `{"host":"host-token","vms":{}}`)
	ingester := &fakeIngester{}
	virtualMachines := &fakeVirtualMachines{ids: []string{"vm-1"}}
	capacity := &fakeHostCapacity{}

	exporter := NewExporter(nil, path, virtualMachines, capacity, time.Second, nil)
	exporter.client = ingester

	exporter.PushAll(context.Background())

	if len(ingester.calls) != 1 {
		t.Fatalf("expected only the host push, got %d calls", len(ingester.calls))
	}
}

func TestPushAllToleratesAMissingBundle(t *testing.T) {
	ingester := &fakeIngester{}
	virtualMachines := &fakeVirtualMachines{ids: []string{"vm-1"}}
	capacity := &fakeHostCapacity{}

	exporter := NewExporter(nil, filepath.Join(t.TempDir(), "missing.json"), virtualMachines, capacity, time.Second, nil)
	exporter.client = ingester

	exporter.PushAll(context.Background())

	if len(ingester.calls) != 0 {
		t.Fatalf("expected no pushes without a bundle, got %d", len(ingester.calls))
	}
}

type blockingIngester struct {
	started chan string
	active  atomic.Int32
	peak    atomic.Int32
}

func (client *blockingIngester) Ingest(ctx context.Context, token string, samples []Sample) error {
	active := client.active.Add(1)
	defer client.active.Add(-1)
	for old := client.peak.Load(); active > old; old = client.peak.Load() {
		if client.peak.CompareAndSwap(old, active) {
			break
		}
	}
	client.started <- token
	<-ctx.Done()
	return ctx.Err()
}

func TestPushAllBoundsConcurrencyAndWaitsForCancellation(t *testing.T) {
	path := writeBundle(t, `{"host":"host","vms":{"vm-1":"one","vm-2":"two","vm-3":"three","vm-4":"four"}}`)
	client := &blockingIngester{started: make(chan string, 8)}
	exporter := NewExporter(nil, path, &fakeVirtualMachines{ids: []string{"vm-1", "vm-2", "vm-3", "vm-4"}}, &fakeHostCapacity{}, time.Minute, nil)
	exporter.client = client
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan struct{})
	go func() { exporter.PushAll(ctx); close(done) }()
	for range maximumConcurrentExports {
		select {
		case <-client.started:
		case <-time.After(time.Second):
			t.Fatal("exports did not run concurrently")
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("export pass did not stop")
	}
	if client.active.Load() != 0 || client.peak.Load() != maximumConcurrentExports {
		t.Fatalf("active=%d peak=%d", client.active.Load(), client.peak.Load())
	}
	if len(client.started) != 0 {
		t.Fatal("started another resource after cancellation")
	}
}

type failingIngester struct{ calls atomic.Int32 }

func (client *failingIngester) Ingest(ctx context.Context, token string, samples []Sample) error {
	client.calls.Add(1)
	if token == "host" {
		return errors.New("unavailable")
	}
	<-ctx.Done()
	return ctx.Err()
}

func TestPushAllContinuesAfterFailuresAndTimesOutEveryResource(t *testing.T) {
	path := writeBundle(t, `{"host":"host","vms":{"vm-1":"one","vm-2":"two","vm-3":"three","vm-4":"four"}}`)
	client := &failingIngester{}
	exporter := NewExporter(nil, path, &fakeVirtualMachines{ids: []string{"vm-1", "vm-2", "vm-3", "vm-4"}}, &fakeHostCapacity{}, 10*time.Millisecond, nil)
	exporter.client = client
	done := make(chan struct{})
	go func() { exporter.PushAll(t.Context()); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("resource timeout did not finish pass")
	}
	if count := client.calls.Load(); count != 5 {
		t.Fatalf("exported %d resources, want 5", count)
	}
}
