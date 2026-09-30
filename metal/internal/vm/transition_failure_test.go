package vm

import (
	"context"
	"errors"
	"testing"
)

func TestFailedRuntimeTransitionReportsActualState(t *testing.T) {
	for _, test := range []struct {
		name         string
		state        State
		inspectError error
		want         State
	}{
		{name: "failed start", state: StateFailed, want: StateFailed},
		{name: "stopped after failure", state: StateStopped, want: StateStopped},
		{name: "still running", state: StateRunning, want: StateRunning},
		{name: "inspection unavailable", state: StateFailed, inspectError: errors.New("inspection failed"), want: StateUnknown},
	} {
		t.Run(test.name, func(t *testing.T) {
			manager, runtime, _, _ := newTestManager(t)
			ctx := context.Background()
			if _, err := manager.Create(ctx, "machine-1", testSpecification()); err != nil {
				t.Fatal(err)
			}
			observed, err := manager.store.readObserved("machine-1")
			if err != nil {
				t.Fatal(err)
			}
			operationError := errors.New("runtime operation failed")
			status, err := manager.runRuntimeTransition(ctx, "machine-1", RuntimeMachine{ID: "machine-1"}, &observed, newOperationID(), phaseStart, StateRunning,
				func(context.Context, RuntimeMachine) error {
					runtime.state = test.state
					runtime.inspectError = test.inspectError
					return operationError
				})
			if !errors.Is(err, operationError) || (test.inspectError != nil && !errors.Is(err, test.inspectError)) {
				t.Fatalf("transition lost its errors: %v", err)
			}
			if status.State != test.want {
				t.Fatalf("returned state = %s, want %s", status.State, test.want)
			}
			stored, err := manager.store.readObserved("machine-1")
			if err != nil {
				t.Fatal(err)
			}
			if stored.State != test.want || stored.Phase != phaseStart || stored.Error == nil || stored.Error.Message != "start operation failed" {
				t.Fatalf("stored observation = %+v", stored)
			}
			if stored.Generation != 0 {
				t.Fatalf("failed transition applied generation %d", stored.Generation)
			}
		})
	}
}
