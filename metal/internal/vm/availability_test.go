package vm

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestAvailabilityCountsOnlyVirtualMachinesTheUserWantsRunning(t *testing.T) {
	running := DesiredRecord{State: StateRunning, Generation: 2}
	sleepy := DesiredRecord{State: StateRunning, Generation: 2, Specification: Specification{SleepAfterIdleSeconds: 300}}
	applied := func(state State) ObservedRecord { return ObservedRecord{State: state, Generation: 2} }
	cases := []struct {
		name       string
		desired    DesiredRecord
		observed   ObservedRecord
		pendingFor time.Duration
		missed     int
		wanted     bool
		reason     string
	}{
		{"running", running, applied(StateRunning), 0, 0, true, ""},
		{"stopped by the user", DesiredRecord{State: StateStopped, Generation: 2}, applied(StateStopped), 0, 0, false, ""},
		{"asleep by design", sleepy, applied(StateStopped), 0, 0, false, ""},
		{"asleep with a stuck change", sleepy, ObservedRecord{State: StateStopped, Generation: 1}, 10 * time.Minute, 0, true, unavailableStuck},
		{"crashed", running, applied(StateStopped), 0, 0, true, unavailableNotRunning},
		{"user change in progress", running, ObservedRecord{State: StateStopped, Generation: 1}, time.Minute, 0, false, ""},
		{"change stuck", running, ObservedRecord{State: StateStopped, Generation: 1}, 10 * time.Minute, 0, true, unavailableStuck},
		{"guest silent", running, applied(StateRunning), 0, missedPingLimit, true, unavailableUnreachable},
		{"one missed ping", running, applied(StateRunning), 0, 1, true, ""},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			wanted, reason := availabilityOf(testCase.desired, testCase.observed, testCase.pendingFor, testCase.missed)
			if wanted != testCase.wanted || reason != testCase.reason {
				t.Fatalf("availability = (%v, %q), want (%v, %q)", wanted, reason, testCase.wanted, testCase.reason)
			}
		})
	}
}

func TestCheckGuestsCountsMissedPingsUntilTheGuestAnswers(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := manager.Create(ctx, "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	runtime.state = StateRunning
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}

	runtime.pingError = errors.New("no reply")
	for range missedPingLimit {
		if err := manager.CheckGuests(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if missed := manager.missedPings("machine-1"); missed != missedPingLimit {
		t.Fatalf("missed pings = %d, want %d", missed, missedPingLimit)
	}

	runtime.pingError = nil
	if err := manager.CheckGuests(ctx); err != nil {
		t.Fatal(err)
	}
	if missed := manager.missedPings("machine-1"); missed != 0 {
		t.Fatalf("missed pings after a reply = %d, want 0", missed)
	}
}
