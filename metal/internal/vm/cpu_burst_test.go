package vm

import (
	"context"
	"errors"
	"testing"
	"time"
)

// TestCPULimit checks the throttle rule for a 1000 millicore baseline on a 2000
// millicore VM: throttle above the baseline, release at 90% of it, and keep the
// state in between.
func TestCPULimit(t *testing.T) {
	burst := CPUBurst{IsBurstable: true, BaselineCPUMillicores: 1000, SlidingWindowMinutes: 60}
	for _, test := range []struct {
		name    string
		current int
		average uint64
		want    int
	}{
		{"released at the baseline stays released", 2000, 1000, 2000},
		{"released above the baseline is throttled", 2000, 1001, 1000},
		{"limited above the release point stays limited", 1000, 901, 1000},
		{"limited at the release point is released", 1000, 900, 2000},
		{"limited at an older baseline gets the current one", 800, 950, 1000},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := cpuLimit(burst, 2000, test.current, test.average); got != test.want {
				t.Fatalf("cpuLimit = %d, want %d", got, test.want)
			}
		})
	}
}

// TestLimitCPUBursts checks that the job skips a VM that is not running, keeps
// the limit of a VM without samples in memory, throttles a busy burstable VM once,
// releases it after a quiet window, and releases it when burst is turned off.
func TestLimitCPUBursts(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.CPUBurst = CPUBurst{IsBurstable: true, BaselineCPUMillicores: 1000, SlidingWindowMinutes: 1}
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	runtime.cpuLimit = 2000
	// The down sample before the window start leaves no missing history, so the
	// one 40 s up pair gives the average.
	averageOverWindow := func(milliCPUs uint64) {
		now := time.Now().Truncate(time.Second)
		manager.slidingWindowUsage.seed("machine-1", time.Minute, []cpuSample{
			{timestamp: now.Add(-70 * time.Second)},
			{timestamp: now.Add(-50 * time.Second), isUp: true},
			{timestamp: now.Add(-10 * time.Second), isUp: true, cpuMicroseconds: milliCPUs * 40_000},
		})
	}
	limit := func(wantLimit, wantSets int) {
		t.Helper()
		if err := manager.LimitCPUBursts(context.Background()); err != nil {
			t.Fatal(err)
		}
		if runtime.cpuLimit != wantLimit || runtime.cpuLimitSets != wantSets {
			t.Fatalf("limit = %d after %d sets, want %d after %d", runtime.cpuLimit, runtime.cpuLimitSets, wantLimit, wantSets)
		}
	}

	averageOverWindow(1500)
	limit(2000, 0)

	observed, err := manager.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	observed.State = StateRunning
	if err := manager.store.writeObserved("machine-1", observed); err != nil {
		t.Fatal(err)
	}

	// After a metald restart, a limited VM has no ring until the next sweep.
	manager.slidingWindowUsage.forget("machine-1")
	runtime.cpuLimit = 1000
	limit(1000, 0)
	runtime.cpuLimit = 2000
	averageOverWindow(1500)

	limit(1000, 1)
	limit(1000, 1)

	averageOverWindow(900)
	limit(2000, 2)

	averageOverWindow(1500)
	limit(1000, 3)
	if err := manager.SetCompute(context.Background(), "machine-1", Compute{CPUMillicores: 2000, MemoryMiB: 2048}); err != nil {
		t.Fatal(err)
	}
	limit(2000, 4)
}

// TestResetCPUBurstHidesEarlierSamplesFromTheLimiter checks that a reset stores
// the time in whole seconds and keeps the current CPU limit, and that the limiter
// then ignores the busy samples before it. A VM that is not burstable cannot reset.
func TestResetCPUBurstHidesEarlierSamplesFromTheLimiter(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.CPUBurst = CPUBurst{IsBurstable: true, BaselineCPUMillicores: 1000, SlidingWindowMinutes: 1}
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	observed, err := manager.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	observed.State = StateRunning
	if err := manager.store.writeObserved("machine-1", observed); err != nil {
		t.Fatal(err)
	}
	runtime.cpuLimit = 2000

	// Without the reset, these samples average 1500 and the VM is limited.
	now := time.Now().Truncate(time.Second)
	manager.slidingWindowUsage.seed("machine-1", time.Minute, []cpuSample{
		{timestamp: now.Add(-70 * time.Second)},
		{timestamp: now.Add(-50 * time.Second), isUp: true},
		{timestamp: now.Add(-10 * time.Second), isUp: true, cpuMicroseconds: 1500 * 40_000},
	})
	if err := manager.ResetCPUBurst(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}

	desired, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if resetAt := desired.CPUBurstResetAt; resetAt.IsZero() || !resetAt.Equal(resetAt.Truncate(time.Second)) {
		t.Fatalf("CPUBurstResetAt = %v, want a time in whole seconds", resetAt)
	}
	if err := manager.LimitCPUBursts(context.Background()); err != nil {
		t.Fatal(err)
	}
	if runtime.cpuLimit != 2000 || runtime.cpuLimitSets != 0 {
		t.Fatalf("limit = %d after %d sets, want 2000 after 0", runtime.cpuLimit, runtime.cpuLimitSets)
	}

	if _, err := manager.Create(context.Background(), "machine-2", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.ResetCPUBurst(context.Background(), "machine-2"); !errors.Is(err, ErrNotBurstable) {
		t.Fatalf("error = %v, want %v", err, ErrNotBurstable)
	}
}
