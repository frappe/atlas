package platform

import (
	"context"
	"syscall"
)

// UnitManager controls the systemd unit that runs one virtual machine.
type UnitManager interface {
	// Start activates the unit and waits for the systemd job to finish.
	Start(ctx context.Context, id string) error

	// Stop deactivates the unit and reports success when the unit is absent.
	Stop(ctx context.Context, id string) error

	// Kill sends signal to every process in the unit.
	Kill(ctx context.Context, id string, signal syscall.Signal) error

	// ResetFailed clears the failed state so the unit can start again.
	ResetFailed(ctx context.Context, id string) error

	// Status reports the current activation state of the unit.
	Status(ctx context.Context, id string) (Status, error)

	// Wait blocks until the unit stops and reports how it stopped.
	Wait(ctx context.Context, id string) (Result, error)

	// List returns the virtual machine IDs that have a unit on this host.
	List(ctx context.Context) ([]string, error)

	// SetLimits applies resource limits to the running unit.
	SetLimits(ctx context.Context, id string, limits Limits) error

	GetUsage(ctx context.Context, id string) (Usage, error)
}

// Status describes one systemd unit.
type Status struct {
	PID         int
	ActiveState string
	SubState    string
}

// Result describes a stopped systemd unit.
type Result struct {
	Code   int
	Signal string
}

// Limits contains systemd resource limits.
type Limits struct {
	MemoryMaxBytes int64
	CPUMillicores  int
}
