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

const slidingWindow = 2 * time.Minute

type cpuSample struct {
	offset     time.Duration
	up         bool
	cpuSeconds uint64
}

// appendCPUSamples stores one metrics record per sample at now plus its offset.
func appendCPUSamples(t *testing.T, manager *Manager, samples ...cpuSample) {
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

func assertMilliCPUs(t *testing.T, manager *Manager, want uint64) {
	t.Helper()
	usage, err := manager.SlidingWindowUsage(context.Background(), slidingWindowVirtualMachineID, slidingWindow)
	if err != nil {
		t.Fatal(err)
	}
	if usage.MilliCPUs != want {
		t.Fatalf("MilliCPUs = %d, want %d", usage.MilliCPUs, want)
	}
}

func assertNotEnoughSamples(t *testing.T, manager *Manager) {
	t.Helper()
	_, err := manager.SlidingWindowUsage(context.Background(), slidingWindowVirtualMachineID, slidingWindow)
	if !errors.Is(err, ErrNotEnoughSamples) {
		t.Fatalf("error = %v, want %v", err, ErrNotEnoughSamples)
	}
}

func TestSlidingWindowUsageAveragesCPUTime(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		cpuSample{-40 * time.Second, true, 10},
		cpuSample{-30 * time.Second, true, 15},
		cpuSample{-20 * time.Second, true, 20},
		cpuSample{-10 * time.Second, true, 25},
	)

	assertMilliCPUs(t, manager, 500)
}

func TestSlidingWindowUsageSkipsDownIntervals(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		cpuSample{-50 * time.Second, true, 0},
		cpuSample{-40 * time.Second, true, 10},
		cpuSample{-30 * time.Second, false, 0},
		cpuSample{-20 * time.Second, true, 0},
		cpuSample{-10 * time.Second, true, 10},
	)

	assertMilliCPUs(t, manager, 1000)
}

func TestSlidingWindowUsageSkipsCounterReset(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		cpuSample{-40 * time.Second, true, 100},
		cpuSample{-30 * time.Second, true, 110},
		cpuSample{-20 * time.Second, true, 2},
		cpuSample{-10 * time.Second, true, 12},
	)

	assertMilliCPUs(t, manager, 1000)
}

func TestSlidingWindowUsageIgnoresRecordsOutsideWindow(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager,
		cpuSample{-5 * time.Minute, true, 0},
		cpuSample{-40 * time.Second, true, 300},
		cpuSample{-30 * time.Second, true, 305},
		cpuSample{-20 * time.Second, true, 310},
	)

	assertMilliCPUs(t, manager, 500)
}

func TestSlidingWindowUsageRequiresTwoSamples(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendCPUSamples(t, manager, cpuSample{-10 * time.Second, true, 10})

	assertNotEnoughSamples(t, manager)
}

func TestSlidingWindowUsageWithoutHistory(t *testing.T) {
	manager, _, _, _ := newTestManager(t)

	assertNotEnoughSamples(t, manager)
}
