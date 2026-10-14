package vm

import (
	"context"
	"testing"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func TestCompletedActionNamesTheUserVisibleChange(t *testing.T) {
	running := ObservedRecord{State: StateRunning, Generation: 2, SpecificationGeneration: 1, RestartGeneration: 1}
	desiredRunning := DesiredRecord{State: StateRunning}
	cases := []struct {
		name     string
		desired  DesiredRecord
		before   ObservedRecord
		after    ObservedRecord
		removed  bool
		expected string
	}{
		{"first boot", desiredRunning, ObservedRecord{State: StateUnknown}, running, false, "create"},
		{"restart", desiredRunning, running, ObservedRecord{State: StateRunning, Generation: 3, SpecificationGeneration: 1, RestartGeneration: 2}, false, "restart"},
		{"shape change", desiredRunning, running, ObservedRecord{State: StateRunning, Generation: 3, SpecificationGeneration: 2, RestartGeneration: 1}, false, "resize"},
		{"idle sleep", desiredRunning, running, ObservedRecord{State: StateStopped, Generation: 2, SpecificationGeneration: 1, RestartGeneration: 1}, false, "sleep"},
		{"stop", DesiredRecord{State: StateStopped}, running, ObservedRecord{State: StateStopped, Generation: 3, SpecificationGeneration: 1, RestartGeneration: 1}, false, "stop"},
		{"metadata", desiredRunning, running, ObservedRecord{State: StateRunning, Generation: 3, SpecificationGeneration: 1, RestartGeneration: 1}, false, "update"},
		{"no change", desiredRunning, running, running, false, ""},
		{"terminate", DesiredRecord{State: StateDestroyed}, running, ObservedRecord{}, true, "terminate"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if action := completedAction(testCase.desired, testCase.before, testCase.after, testCase.removed); action != testCase.expected {
				t.Fatalf("action = %q, want %q", action, testCase.expected)
			}
		})
	}
}

func TestTakeRequestIgnoresARequestForAnotherGeneration(t *testing.T) {
	manager := &Manager{requests: make(map[string]notedRequest)}
	ctx := telemetry.WithRequestID(context.Background(), "request-1")
	manager.noteRequest(ctx, DesiredRecord{ID: "vm-1-0001", Generation: 2, RestartGeneration: 1})

	// The API applied generation 2 itself, so a later pass applies generation 3.
	if requestID := manager.takeRequest(DesiredRecord{ID: "vm-1-0001", Generation: 3, RestartGeneration: 1}); requestID != "" {
		t.Fatalf("request ID = %q, want none for another generation", requestID)
	}

	manager.noteRequest(ctx, DesiredRecord{ID: "vm-1-0001", Generation: 3, RestartGeneration: 1})
	if requestID := manager.takeRequest(DesiredRecord{ID: "vm-1-0001", Generation: 3, RestartGeneration: 1}); requestID != "request-1" {
		t.Fatalf("request ID = %q, want request-1", requestID)
	}
}

func TestInventoryCountsSleepyVirtualMachinesByState(t *testing.T) {
	sleepy := Specification{SleepAfterIdleSeconds: 300}
	records := []struct {
		desired  DesiredRecord
		observed ObservedRecord
	}{
		{DesiredRecord{State: StateRunning, Generation: 1, Specification: sleepy}, ObservedRecord{State: StateStopped, Generation: 1}},
		{DesiredRecord{State: StateStopped, Generation: 1, Specification: sleepy}, ObservedRecord{State: StateStopped, Generation: 1}},
		{DesiredRecord{State: StateRunning, Generation: 1}, ObservedRecord{State: StateRunning, Generation: 1}},
	}
	manager := &Manager{}
	counts := newInventory()
	for _, record := range records {
		manager.addToInventory(&counts, record.desired, record.observed)
	}

	if counts.sleepyStates["sleeping"] != 1 || counts.sleepyStates["stopped"] != 1 || counts.sleepyStates["running"] != 0 {
		t.Fatalf("sleepy states = %v, want one sleeping and one stopped", counts.sleepyStates)
	}
	if counts.states["sleeping"] != 1 || counts.states["running"] != 1 {
		t.Fatalf("states = %v, want the asleep VM and the running VM", counts.states)
	}
}
