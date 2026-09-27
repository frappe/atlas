package datum

import (
	"context"
	"os"
	"path/filepath"
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
	calls []ingestCall
}

type ingestCall struct {
	token   string
	samples []Sample
}

func (f *fakeIngester) Ingest(_ context.Context, token string, samples []Sample) error {
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
	if ingester.calls[0].token != "host-token" {
		t.Fatalf("first push token = %q", ingester.calls[0].token)
	}
	if ingester.calls[1].token != "vm-1-token" {
		t.Fatalf("second push token = %q, vm-2 has no token and must be skipped", ingester.calls[1].token)
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
