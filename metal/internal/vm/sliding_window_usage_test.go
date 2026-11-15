package vm

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/metrics"
)

const slidingWindowVirtualMachineID = "sliding-window"

type storedCPUSample struct {
	offset     time.Duration
	up         bool
	cpuSeconds uint64
}

// appendCPUSamples stores one metrics record per sample at now plus its offset.
func appendCPUSamples(t *testing.T, manager *Manager, samples ...storedCPUSample) {
	t.Helper()
	if err := os.Mkdir(filepath.Join(manager.configuration.MachinesDirectory, slidingWindowVirtualMachineID), 0o700); err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	for _, sample := range samples {
		record := metrics.Record{Timestamp: now.Add(sample.offset), Metrics: metrics.Sample{
			Up:           sample.up,
			ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: sample.cpuSeconds * 1_000_000},
		}}
		if err := manager.metrics.Append(slidingWindowVirtualMachineID, record); err != nil {
			t.Fatal(err)
		}
	}
}

// assertMilliCPUs checks the average CPU use over the manager's sliding window.
func assertMilliCPUs(t *testing.T, manager *Manager, want uint64) {
	t.Helper()
	usage, err := manager.SlidingWindowUsage(context.Background(), slidingWindowVirtualMachineID)
	if err != nil {
		t.Fatal(err)
	}
	if usage.MilliCPUs != want {
		t.Fatalf("MilliCPUs = %d, want %d", usage.MilliCPUs, want)
	}
}

// assertNotEnoughSamples checks that the sliding window has no usable sample pair.
func assertNotEnoughSamples(t *testing.T, manager *Manager) {
	t.Helper()
	_, err := manager.SlidingWindowUsage(context.Background(), slidingWindowVirtualMachineID)
	if !errors.Is(err, ErrNotEnoughSamples) {
		t.Fatalf("error = %v, want %v", err, ErrNotEnoughSamples)
	}
}

// TestSlidingWindowUsageAveragesCPUTime checks that 5 CPU seconds in each
// 10-second interval gives 500 milli-CPUs.
func TestSlidingWindowUsageAveragesCPUTime(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, true, 15},
		storedCPUSample{-20 * time.Second, true, 20},
		storedCPUSample{-10 * time.Second, true, 25},
	)

	assertMilliCPUs(t, manager, 500)
}

// TestSlidingWindowUsageSkipsDownIntervals checks that intervals next to a down
// sample count as neither CPU time nor up time.
func TestSlidingWindowUsageSkipsDownIntervals(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		storedCPUSample{-50 * time.Second, true, 0},
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, false, 0},
		storedCPUSample{-20 * time.Second, true, 0},
		storedCPUSample{-10 * time.Second, true, 10},
	)

	assertMilliCPUs(t, manager, 1000)
}

// TestSlidingWindowUsageSkipsCounterReset checks that a falling CPU counter,
// from a restart, drops that interval instead of underflowing.
func TestSlidingWindowUsageSkipsCounterReset(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 100},
		storedCPUSample{-30 * time.Second, true, 110},
		storedCPUSample{-20 * time.Second, true, 2},
		storedCPUSample{-10 * time.Second, true, 12},
	)

	assertMilliCPUs(t, manager, 1000)
}

// TestSlidingWindowUsageIgnoresRecordsOutsideWindow checks that samples older
// than the window do not change the average.
func TestSlidingWindowUsageIgnoresRecordsOutsideWindow(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		storedCPUSample{-61 * time.Minute, true, 0},
		storedCPUSample{-40 * time.Second, true, 300},
		storedCPUSample{-30 * time.Second, true, 305},
		storedCPUSample{-20 * time.Second, true, 310},
	)

	assertMilliCPUs(t, manager, 500)
}

// TestSlidingWindowUsageRequiresTwoSamples checks that one sample returns
// ErrNotEnoughSamples.
func TestSlidingWindowUsageRequiresTwoSamples(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager, storedCPUSample{-10 * time.Second, true, 10})

	assertNotEnoughSamples(t, manager)
}

// TestSlidingWindowUsageWithoutHistory checks that a VM without samples
// returns ErrNotEnoughSamples.
func TestSlidingWindowUsageWithoutHistory(t *testing.T) {
	manager, _, _, _ := newTestManager(t)

	assertNotEnoughSamples(t, manager)
}

// TestSlidingWindowUsageSkipsSamplesWithoutElapsedTime checks that two samples
// in the same second add no CPU time without up time.
func TestSlidingWindowUsageSkipsSamplesWithoutElapsedTime(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		storedCPUSample{-30 * time.Second, true, 0},
		storedCPUSample{-20 * time.Second, true, 5},
		storedCPUSample{-20 * time.Second, true, 15},
		storedCPUSample{-10 * time.Second, true, 20},
	)

	assertMilliCPUs(t, manager, 500)
}
