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

const testBaselineMillicores = 400

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

// newTestUsage returns a ring with a 30-second window, so it holds 4 samples,
// built from samples in order.
func newTestUsage(samples ...cpuSample) *slidingWindowUsage {
	usage := newSlidingWindowUsage()
	usage.seed(testVirtualMachineID, 30*time.Second, samples)
	return usage
}

// newBurstableManager returns a manager with one burstable VM that averages over windowMinutes.
func newBurstableManager(t *testing.T, windowMinutes int) *Manager {
	t.Helper()
	manager, _, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.CPUBurst = CPUBurst{IsBurstable: true, BaselineCPUMillicores: testBaselineMillicores, SlidingWindowMinutes: windowMinutes}
	if _, err := manager.Create(context.Background(), testVirtualMachineID, specification); err != nil {
		t.Fatal(err)
	}
	return manager
}

// assertAverage checks the average at now, with testBaselineMillicores, against wantMilliCPUs.
func assertAverage(t *testing.T, usage *slidingWindowUsage, now time.Time, wantMilliCPUs uint64) {
	t.Helper()
	if got := usage.average(testVirtualMachineID, now, testBaselineMillicores); got.MilliCPUs != wantMilliCPUs {
		t.Fatalf("MilliCPUs = %d, want %d", got.MilliCPUs, wantMilliCPUs)
	}
}

// storedCPUSample is one record for the store, placed relative to time.Now.
type storedCPUSample struct {
	offset     time.Duration
	up         bool
	cpuSeconds uint64
}

// appendStoredSamples writes records to the store without recording them in the
// ring, and returns the last record.
func appendStoredSamples(t *testing.T, manager *Manager, samples ...storedCPUSample) metrics.Record {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(manager.configuration.MachinesDirectory, testVirtualMachineID), 0o700); err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	var record metrics.Record
	for _, sample := range samples {
		record = metrics.Record{Timestamp: now.Add(sample.offset), Metrics: metrics.Sample{
			Up:           sample.up,
			ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: sample.cpuSeconds * 1_000_000},
		}}
		if err := manager.metrics.Append(testVirtualMachineID, record); err != nil {
			t.Fatal(err)
		}
	}
	return record
}

// recordSamples stores each sample and then gives it to RecordMetrics, as the sampler does.
func recordSamples(t *testing.T, manager *Manager, samples ...storedCPUSample) {
	t.Helper()
	for _, sample := range samples {
		record := appendStoredSamples(t, manager, sample)
		if err := manager.RecordMetrics(context.Background(), testVirtualMachineID, record); err != nil {
			t.Fatal(err)
		}
	}
}

// assertRing checks the window and sample count of testVirtualMachineID's ring.
// Manager tests check the ring, not the average, because a ring refilled from
// the store starts inside the window by a wall-clock-dependent amount.
func assertRing(t *testing.T, manager *Manager, wantWindow time.Duration, wantCount int) {
	t.Helper()
	ring := manager.slidingWindowUsage.rings[testVirtualMachineID]
	if ring == nil || ring.window != wantWindow || ring.count != wantCount {
		t.Fatalf("ring = %+v, want window %v with %d samples", ring, wantWindow, wantCount)
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

	assertAverage(t, usage, secondsAfterBase(40), 500)
}

// TestSlidingWindowUsageUsesPairAcrossMissedSweep checks that a missed sweep
// gives one longer pair that still counts.
func TestSlidingWindowUsageUsesPairAcrossMissedSweep(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(30, 10))

	assertAverage(t, usage, secondsAfterBase(40), 500)
}

// TestSlidingWindowUsageIgnoresSamplesOutsideWindow checks that the time filter
// drops samples that the ring still holds after missed sweeps. The ring keeps
// t30 t40 t70 t80, and at t90 only t70 to t80 is inside the window; without the
// filter the average would be 1100.
func TestSlidingWindowUsageIgnoresSamplesOutsideWindow(t *testing.T) {
	usage := newTestUsage(upSample(20, 0), upSample(30, 0), upSample(40, 0), upSample(70, 50), upSample(80, 55))

	assertAverage(t, usage, secondsAfterBase(90), 500)
}

// TestSlidingWindowUsageReturnsBaselineWithoutUpTime checks that a VM with no
// ring, one sample, or only down samples over the window averages at the baseline.
func TestSlidingWindowUsageReturnsBaselineWithoutUpTime(t *testing.T) {
	assertAverage(t, newSlidingWindowUsage(), secondsAfterBase(10), testBaselineMillicores)
	assertAverage(t, newTestUsage(upSample(10, 10)), secondsAfterBase(10), testBaselineMillicores)
	assertAverage(t, newTestUsage(downSample(10), downSample(20), downSample(30), downSample(40)), secondsAfterBase(40), testBaselineMillicores)
}

// TestSlidingWindowUsageForgetsVM checks that forget drops the VM's ring.
func TestSlidingWindowUsageForgetsVM(t *testing.T) {
	usage := newTestUsage(upSample(10, 0), upSample(20, 10))
	usage.forget(testVirtualMachineID)

	if len(usage.rings) != 0 {
		t.Fatalf("rings = %d, want 0", len(usage.rings))
	}
}

// TestSlidingWindowUsageRecordNeedsARing checks that record builds nothing for a
// VM without a ring, or with a ring for another window, and asks for a refill.
func TestSlidingWindowUsageRecordNeedsARing(t *testing.T) {
	usage := newSlidingWindowUsage()
	if usage.record(testVirtualMachineID, 30*time.Second, upSample(10, 0)) || len(usage.rings) != 0 {
		t.Fatalf("record without a ring: rings = %d, want 0 and false", len(usage.rings))
	}

	usage = newTestUsage(upSample(10, 0))
	if usage.record(testVirtualMachineID, time.Minute, upSample(20, 0)) || usage.rings[testVirtualMachineID].count != 1 {
		t.Fatal("record into a ring for another window")
	}
}

// TestSlidingWindowUsageSeedsFromStoreAfterRestart checks that the first record
// after a metald restart fills the ring from the store, so earlier samples count.
func TestSlidingWindowUsageSeedsFromStoreAfterRestart(t *testing.T) {
	manager := newBurstableManager(t, 60)
	appendStoredSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, true, 15},
		storedCPUSample{-20 * time.Second, true, 20},
	)
	recordSamples(t, manager, storedCPUSample{-10 * time.Second, true, 25})

	assertRing(t, manager, time.Hour, 4)
}

// TestSlidingWindowUsageReadsTheStoreOnce checks that once the ring exists,
// records go only to memory: the average survives removal of the stored files.
func TestSlidingWindowUsageReadsTheStoreOnce(t *testing.T) {
	manager := newBurstableManager(t, 60)
	recordSamples(t, manager,
		storedCPUSample{-40 * time.Second, true, 10},
		storedCPUSample{-30 * time.Second, true, 15},
	)
	if err := os.RemoveAll(filepath.Join(manager.configuration.MachinesDirectory, testVirtualMachineID, "metrics")); err != nil {
		t.Fatal(err)
	}

	record := metrics.Record{Timestamp: time.Now().UTC().Add(-20 * time.Second), Metrics: metrics.Sample{
		Up: true, ComputeUsage: metrics.ComputeUsage{CPUTimeMicroseconds: 20_000_000},
	}}
	if err := manager.RecordMetrics(context.Background(), testVirtualMachineID, record); err != nil {
		t.Fatal(err)
	}

	assertRing(t, manager, time.Hour, 3)
}

// TestSlidingWindowUsageRebuildsRingWhenWindowChanges checks that after the
// window grows, the next record refills the ring from the store, so it gets the
// older samples: 5 samples in 2 minutes instead of 2 in 1 minute.
func TestSlidingWindowUsageRebuildsRingWhenWindowChanges(t *testing.T) {
	manager := newBurstableManager(t, 1)
	appendStoredSamples(t, manager,
		storedCPUSample{-110 * time.Second, true, 0},
		storedCPUSample{-100 * time.Second, true, 20},
		storedCPUSample{-40 * time.Second, true, 20},
	)
	recordSamples(t, manager, storedCPUSample{-30 * time.Second, true, 25})
	assertRing(t, manager, time.Minute, 2)

	burst := CPUBurst{IsBurstable: true, BaselineCPUMillicores: testBaselineMillicores, SlidingWindowMinutes: 2}
	if err := manager.SetCompute(context.Background(), testVirtualMachineID, Compute{CPUMillicores: 2000, MemoryMiB: 2048, CPUBurst: burst}); err != nil {
		t.Fatal(err)
	}
	recordSamples(t, manager, storedCPUSample{-25 * time.Second, true, 25})

	assertRing(t, manager, 2*time.Minute, 5)
}

// TestSlidingWindowUsageNeedsABurstableVM checks that an unknown VM returns
// ErrNotFound, a VM that is not burstable returns ErrNotBurstable, and turning
// burst off drops the ring at the next record.
func TestSlidingWindowUsageNeedsABurstableVM(t *testing.T) {
	manager := newBurstableManager(t, 60)
	recordSamples(t, manager,
		storedCPUSample{-20 * time.Second, true, 0},
		storedCPUSample{-10 * time.Second, true, 10},
	)

	if err := manager.SetCompute(context.Background(), testVirtualMachineID, Compute{CPUMillicores: 2000, MemoryMiB: 2048}); err != nil {
		t.Fatal(err)
	}
	recordSamples(t, manager, storedCPUSample{0, true, 20})

	if _, err := manager.SlidingWindowUsage("missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing VM error = %v, want %v", err, ErrNotFound)
	}
	if _, err := manager.SlidingWindowUsage(testVirtualMachineID); !errors.Is(err, ErrNotBurstable) {
		t.Fatalf("error = %v, want %v", err, ErrNotBurstable)
	}
	if len(manager.slidingWindowUsage.rings) != 0 {
		t.Fatalf("rings = %d, want 0", len(manager.slidingWindowUsage.rings))
	}
}
