package vm

import (
	"context"
	"errors"
	"testing"
)

func TestMetricsReportsUsageForARunningVirtualMachine(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	runtime.usage = Usage{MemoryBytes: 1073741824, CPUUsageMicroseconds: 42_000_000}

	metrics, err := manager.Metrics(context.Background(), "machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if metrics.DiskMiB != 4096 {
		t.Fatalf("DiskMiB = %d, want 4096", metrics.DiskMiB)
	}
	if metrics.MemoryBytes != 1073741824 || metrics.CPUUsageMicroseconds != 42_000_000 {
		t.Fatalf("metrics = %+v", metrics)
	}
	// No traffic attachment was set up, so counters read as zero rather than an error.
	if metrics.ReceivedBytes != 0 || metrics.SentBytes != 0 {
		t.Fatalf("expected zero network counters without an attachment, got %+v", metrics)
	}
}

func TestMetricsReportsOnlyDiskForAStoppedVirtualMachine(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	runtime.usage = Usage{MemoryBytes: 1073741824, CPUUsageMicroseconds: 42_000_000}

	metrics, err := manager.Metrics(context.Background(), "machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if metrics.DiskMiB != 4096 {
		t.Fatalf("DiskMiB = %d, want 4096", metrics.DiskMiB)
	}
	if metrics.MemoryBytes != 0 || metrics.CPUUsageMicroseconds != 0 {
		t.Fatalf("a stopped VM must report no live usage, got %+v", metrics)
	}
}

func TestMetricsRejectsAMissingVirtualMachine(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Metrics(context.Background(), "missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}
