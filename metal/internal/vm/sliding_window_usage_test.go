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

const testVirtualMachineID = "sliding-window"

// baseTime is a fixed clock, so ring-level tests never read time.Now.
var baseTime = time.Unix(1_700_000_000, 0).UTC()

func secondsAfterBase(seconds int) time.Time {
	return baseTime.Add(time.Duration(seconds) * time.Second)
}

func upSample(seconds int, cpuSeconds uint64) cpuSample {
	return cpuSample{timestamp: secondsAfterBase(seconds), isUp: true, cpuMicroseconds: cpuSeconds * 1_000_000}
}

func downSample(seconds int) cpuSample {
	return cpuSample{timestamp: secondsAfterBase(seconds)}
}

// newTestUsage returns a 30-second window, so each ring holds 4 samples, with
// samples recorded in order.
func newTestUsage(samples ...cpuSample) *slidingWindowUsage {
	usage := newSlidingWindowUsage(30 * time.Second)
	for _, sample := range samples {
		usage.record(testVirtualMachineID, sample)
	}
	return usage
}

// assertAverage checks the average at now against wantMilliCPUs.
func assertAverage(t *testing.T, usage *slidingWindowUsage, now time.Time, wantMilliCPUs uint64) {
	t.Helper()
	got, err := usage.average(testVirtualMachineID, now)
	if err != nil {
		t.Fatal(err)
	}
	if got.MilliCPUs != wantMilliCPUs {
		t.Fatalf("MilliCPUs = %d, want %d", got.MilliCPUs, wantMilliCPUs)
	}
}

// assertNotEnoughSamples checks that the average at now has no usable pair.
func assertNotEnoughSamples(t *testing.T, usage *slidingWindowUsage, now time.Time) {
	t.Helper()
	if _, err := usage.average(testVirtualMachineID, now); !errors.Is(err, ErrNotEnoughSamples) {
		t.Fatalf("error = %v, want %v", err, ErrNotEnoughSamples)
	}
}

// storedCPUSample is one record for the store, placed relative to time.Now.
type storedCPUSample struct {
	offset     time.Duration
	up         bool
	cpuSeconds uint64
}

// appendStoredSamples writes records to the store, as the sampler does, without
// recording them in the ring.
func appendStoredSamples(t *testing.T, manager *Manager, samples ...storedCPUSample) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(manager.configuration.MachinesDirectory, testVirtualMachineID), 0o700); err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	for _, sample := range samples {
		record := metrics.Record{Timestamp: now.Add(sample.offset), Metrics: metrics.Sample{
			Up:           sample.up,
			ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: sample.cpuSeconds * 1_000_000},
		}}
		if err := manager.metrics.Append(testVirtualMachineID, record); err != nil {
			t.Fatal(err)
		}
	}
}

// assertManagerMilliCPUs checks the manager's sliding window average for testVirtualMachineID.
func assertManagerMilliCPUs(t *testing.T, manager *Manager, wantMilliCPUs uint64) {
	t.Helper()
	got, err := manager.SlidingWindowUsage(context.Background(), testVirtualMachineID)
	if err != nil {
		t.Fatal(err)
	}
	if got.MilliCPUs != wantMilliCPUs {
		t.Fatalf("MilliCPUs = %d, want %d", got.MilliCPUs, wantMilliCPUs)
	}
}

// TestCPUSampleRingKeepsNewestSamplesInOrder checks that a full ring overwrites
// its oldest sample and still reads oldest first.
func TestCPUSampleRingKeepsNewestSamplesInOrder(t *testing.T) {
	ring := &cpuSampleRing{samples: make([]cpuSample, 4)}
	for _, seconds := range []int{10, 20, 30, 40, 50} {
		ring.push(upSample(seconds, 0))
	}

	for index, seconds := range []int{20, 30, 40, 50} {
		if got := ring.at(index).timestamp; !got.Equal(secondsAfterBase(seconds)) {
			t.Fatalf("at(%d) = %v, want %v", index, got, secondsAfterBase(seconds))
		}
	}
}

// TestSlidingWindowUsageAveragesCPUTime checks that 5 CPU seconds in each
// 10-second interval gives 500 milli-CPUs.
func TestSlidingWindowUsageAveragesCPUTime(t *testing.T) {
	usage := newTestUsage(upSample(10, 10), upSample(20, 15), upSample(30, 20), upSample(40, 25))

	assertAverage(t, usage, secondsAfterBase(40), 500)
}

// TestSlidingWindowUsageSkipsDownIntervals checks that a down sample skips both
// of its pairs, so time the VM was down does not lower the average.
func TestSlidingWindowUsageSkipsDownIntervals(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(20, 10), downSample(30), upSample(40, 0))

	assertAverage(t, usage, secondsAfterBase(40), 1000)
}

// TestSlidingWindowUsageCountsNewCgroupAfterCounterReset checks that when the
// counter falls, the new cgroup's whole counter counts as CPU used in that
// interval: 10 + 2 + 10 CPU seconds over 30 seconds.
func TestSlidingWindowUsageCountsNewCgroupAfterCounterReset(t *testing.T) {
	usage := newTestUsage(upSample(10, 100), upSample(20, 110), upSample(30, 2), upSample(40, 12))

	assertAverage(t, usage, secondsAfterBase(40), 733)
}

// TestSlidingWindowUsageSkipsSamplesWithoutElapsedTime checks that two samples
// in the same second add no CPU time without up time.
func TestSlidingWindowUsageSkipsSamplesWithoutElapsedTime(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(20, 5), upSample(20, 15), upSample(30, 20))

	assertAverage(t, usage, secondsAfterBase(30), 500)
}

// TestSlidingWindowUsageUsesPairAcrossMissedSweep checks that a missed sweep
// gives one longer pair that still counts.
func TestSlidingWindowUsageUsesPairAcrossMissedSweep(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(30, 10))

	assertAverage(t, usage, secondsAfterBase(30), 500)
}

// TestSlidingWindowUsageIgnoresSamplesOutsideWindow checks that the time filter
// drops samples that the ring still holds after missed sweeps. The ring keeps
// t30 t40 t70 t80, and at t90 only t70 to t80 is inside the window; without the
// filter the average would be 1100.
func TestSlidingWindowUsageIgnoresSamplesOutsideWindow(t *testing.T) {
	usage := newTestUsage(upSample(20, 0), upSample(30, 0), upSample(40, 0), upSample(70, 50), upSample(80, 55))

	assertAverage(t, usage, secondsAfterBase(90), 500)
}

// TestSlidingWindowUsageRequiresTwoSamples checks that a VM with one sample, or
// none, returns ErrNotEnoughSamples.
func TestSlidingWindowUsageRequiresTwoSamples(t *testing.T) {
	assertNotEnoughSamples(t, newTestUsage(upSample(10, 10)), secondsAfterBase(10))
	assertNotEnoughSamples(t, newTestUsage(), secondsAfterBase(10))
}

// TestSlidingWindowUsageForgetsVM checks that a forgotten VM has no samples.
func TestSlidingWindowUsageForgetsVM(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(20, 10))
	usage.forget(testVirtualMachineID)

	assertNotEnoughSamples(t, usage, secondsAfterBase(20))
}

// TestSlidingWindowUsageSeedKeepsNewerRecordedSamples checks that seeding keeps
// a sample recorded while History read the file, and does not duplicate the
// sample that is both stored and recorded.
func TestSlidingWindowUsageSeedKeepsNewerRecordedSamples(t *testing.T) {
	usage := newTestUsage(upSample(30, 0), upSample(40, 0))
	usage.seed(testVirtualMachineID, []cpuSample{upSample(10, 0), upSample(20, 0), upSample(30, 0)})

	ring := usage.rings[testVirtualMachineID]
	if !ring.isSeeded || ring.count != 4 {
		t.Fatalf("isSeeded = %t, count = %d, want true and 4", ring.isSeeded, ring.count)
	}
	for index, seconds := range []int{10, 20, 30, 40} {
		if got := ring.at(index).timestamp; !got.Equal(secondsAfterBase(seconds)) {
			t.Fatalf("at(%d) = %v, want %v", index, got, secondsAfterBase(seconds))
		}
	}
}

// TestSlidingWindowUsageSeedsFromStoreAfterRestart checks that a manager with
// no samples in memory reads the stored samples on its first call.
func TestSlidingWindowUsageSeedsFromStoreAfterRestart(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendStoredSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, true, 15},
		storedCPUSample{-20 * time.Second, true, 20},
		storedCPUSample{-10 * time.Second, true, 25},
	)

	assertManagerMilliCPUs(t, manager, 500)
}

// TestSlidingWindowUsageSeedsOnlyOnce checks that after the first call, only
// recorded samples change the result. A sample written only to the store, which
// would raise the average to 1000, is not read again.
func TestSlidingWindowUsageSeedsOnlyOnce(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	appendStoredSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, true, 15},
		storedCPUSample{-20 * time.Second, true, 20},
		storedCPUSample{-10 * time.Second, true, 25},
	)
	assertManagerMilliCPUs(t, manager, 500)

	appendStoredSamples(t, manager, storedCPUSample{-5 * time.Second, true, 45})

	assertManagerMilliCPUs(t, manager, 500)
}

// TestSlidingWindowUsageRecordsSampledMetrics checks that samples from
// RecordMetrics give an average when the store has none.
func TestSlidingWindowUsageRecordsSampledMetrics(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	now := time.Now().UTC()
	for _, record := range []metrics.Record{
		{Timestamp: now.Add(-20 * time.Second), Metrics: metrics.Sample{Up: true}},
		{Timestamp: now.Add(-10 * time.Second), Metrics: metrics.Sample{Up: true, ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: 10_000_000}}},
	} {
		manager.RecordMetrics(testVirtualMachineID, record)
	}

	assertManagerMilliCPUs(t, manager, 1000)
}

// TestSlidingWindowUsageMakesNoRingForUnknownVM checks that asking for a VM
// without samples leaves no ring behind.
func TestSlidingWindowUsageMakesNoRingForUnknownVM(t *testing.T) {
	manager, _, _, _ := newTestManager(t)

	_, err := manager.SlidingWindowUsage(context.Background(), testVirtualMachineID)
	if !errors.Is(err, ErrNotEnoughSamples) {
		t.Fatalf("error = %v, want %v", err, ErrNotEnoughSamples)
	}
	if len(manager.slidingWindowUsage.rings) != 0 {
		t.Fatalf("rings = %d, want 0", len(manager.slidingWindowUsage.rings))
	}
}
