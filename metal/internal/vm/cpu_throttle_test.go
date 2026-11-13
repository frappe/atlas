package vm

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/metrics"
)

// windowWithUsage returns a window in which identifier used averageMillicores for ten minutes.
func windowWithUsage(identifier string, averageMillicores int) *metrics.CPUWindow {
	window := metrics.NewCPUWindow(time.Hour)
	now := time.Now().UTC()
	window.Add(identifier, metrics.Record{Timestamp: now.Add(-10 * time.Minute), Metrics: metrics.Sample{Up: true}})
	window.Add(identifier, metrics.Record{Timestamp: now, Metrics: metrics.Sample{
		Up: true, ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: uint64(averageMillicores) * 600_000},
	}})
	return window
}

func runningVirtualMachine(t *testing.T, manager *Manager, specification Specification) {
	t.Helper()
	ctx := context.Background()
	if _, err := manager.Create(ctx, "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if information, err := manager.Information(ctx, "machine-1"); err != nil || information.State != StateRunning {
		t.Fatalf("information = %+v, %v", information, err)
	}
}

func TestCPUThrottlerHoldsABurstableVirtualMachineAtItsBaseline(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.IsBurstable = true
	runningVirtualMachine(t, manager, specification)
	throttler := NewCPUThrottler(manager, nil)

	// The default baseline of 2000 millicores is 400. The release point is 360.
	for _, step := range []struct {
		averageMillicores int
		wantLimit         int
	}{
		{300, 2000},
		{1000, 400},
		{380, 400},
		{350, 2000},
	} {
		throttler.window = windowWithUsage("machine-1", step.averageMillicores)
		if err := throttler.Throttle(context.Background()); err != nil {
			t.Fatal(err)
		}
		if runtime.cpuLimitMillicores != step.wantLimit {
			t.Fatalf("average %d: CPU limit = %d, want %d", step.averageMillicores, runtime.cpuLimitMillicores, step.wantLimit)
		}
	}
}

func TestCPUThrottlerLeavesAVirtualMachineThatIsNotBurstable(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	runningVirtualMachine(t, manager, testSpecification())

	if err := NewCPUThrottler(manager, windowWithUsage("machine-1", 2000)).Throttle(context.Background()); err != nil {
		t.Fatal(err)
	}
	if runtime.cpuLimitMillicores != 0 {
		t.Fatalf("CPU limit = %d, want no change", runtime.cpuLimitMillicores)
	}
}

func TestCPUThrottlerReleasesAVirtualMachineAfterAReset(t *testing.T) {
	ctx := context.Background()
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.IsBurstable = true
	runningVirtualMachine(t, manager, specification)
	throttler := NewCPUThrottler(manager, windowWithUsage("machine-1", 1000))
	if err := throttler.Throttle(ctx); err != nil || runtime.cpuLimitMillicores != 400 {
		t.Fatalf("CPU limit = %d, %v; want 400", runtime.cpuLimitMillicores, err)
	}

	if err := manager.ResetCPUThrottle(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := throttler.Throttle(ctx); err != nil || runtime.cpuLimitMillicores != 2000 {
		t.Fatalf("CPU limit after reset = %d, %v; want 2000", runtime.cpuLimitMillicores, err)
	}
}

func TestResetCPUThrottleNeedsABurstableVirtualMachine(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	runningVirtualMachine(t, manager, testSpecification())

	if err := manager.ResetCPUThrottle(context.Background(), "machine-1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("error = %v, want ErrConflict", err)
	}
}
