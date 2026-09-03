package firecracker

import (
	"context"

	"github.com/frappe/atlas/metal/internal/vm"
)

type transition func(ctx context.Context, machine *machine) error

func start(ctx context.Context, machine *machine) error  { return machine.startUnlocked(ctx) }
func stop(ctx context.Context, machine *machine) error   { return machine.stopUnlocked(ctx) }
func pause(ctx context.Context, machine *machine) error  { return machine.pauseUnlocked(ctx) }
func resume(ctx context.Context, machine *machine) error { return machine.resumeUnlocked(ctx) }

type edge struct{ desired, observed vm.State }

var transitions = map[edge]transition{
	{vm.StateRunning, vm.StateCreated}: start,
	{vm.StateRunning, vm.StateStopped}: start,
	{vm.StateRunning, vm.StateFailed}:  start,
	{vm.StateRunning, vm.StatePaused}:  resume,

	{vm.StateStopped, vm.StateCreated}: stop,
	{vm.StateStopped, vm.StateRunning}: stop,
	{vm.StateStopped, vm.StatePaused}:  stop,
	{vm.StateStopped, vm.StateFailed}:  stop,

	{vm.StatePaused, vm.StateRunning}: pause,
	{vm.StatePaused, vm.StateCreated}: start,
	{vm.StatePaused, vm.StateStopped}: start,
	{vm.StatePaused, vm.StateFailed}:  start,
}

func advance(ctx context.Context, machine *machine, desired, observed vm.State) error {
	if desired == observed {
		return nil
	}
	if step, ok := transitions[edge{desired, observed}]; ok {
		return step(ctx, machine)
	}
	return &vm.TransitionError{DesiredState: desired, ObservedState: observed}
}
