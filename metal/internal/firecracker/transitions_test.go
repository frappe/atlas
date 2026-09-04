package firecracker

import (
	"context"
	"errors"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestAdvanceNoopWhenConverged(t *testing.T) {
	for _, target := range []vm.State{vm.StateRunning, vm.StateStopped, vm.StatePaused} {
		if err := advance(context.Background(), nil, target, target); err != nil {
			t.Errorf("advance at target %s: %v", target, err)
		}
	}
}

func TestTransitionsCoverEveryStep(t *testing.T) {
	want := []edge{
		{vm.StateRunning, vm.StateCreated},
		{vm.StateRunning, vm.StateStopped},
		{vm.StateRunning, vm.StateFailed},
		{vm.StateRunning, vm.StatePaused},
		{vm.StateStopped, vm.StateRunning},
		{vm.StateStopped, vm.StatePaused},
		{vm.StatePaused, vm.StateRunning},
	}
	for _, transitionEdge := range want {
		if _, ok := transitions[transitionEdge]; !ok {
			t.Errorf("missing transition for desired %s from %s", transitionEdge.desired, transitionEdge.observed)
		}
	}
}

func TestAdvanceRejectsUnknownState(t *testing.T) {
	err := advance(context.Background(), nil, vm.StateRunning, vm.StateUnknown)
	var transitionError *vm.TransitionError
	if !errors.As(err, &transitionError) {
		t.Fatalf("advance error = %v, want TransitionError", err)
	}
	if !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("advance error = %v, want ErrConflict", err)
	}
}
