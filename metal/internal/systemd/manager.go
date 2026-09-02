// Package systemd is metald's thin client over systemd via D-Bus. Each VM is the
// template instance metal-vm@<id>.service.
package systemd

import (
	"context"
	"syscall"
)

type Manager interface {
	Start(ctx context.Context, id string) error
	Stop(ctx context.Context, id string) error
	Kill(ctx context.Context, id string, sig syscall.Signal) error
	// ResetFailed clears a unit's failed state, so it reports inactive again.
	ResetFailed(ctx context.Context, id string) error
	Status(ctx context.Context, id string) (Status, error)
	// Wait blocks until the unit goes inactive/failed and returns its result.
	Wait(ctx context.Context, id string) (Result, error)
	// List returns the ids of all metal-vm@*.service instances.
	List(ctx context.Context) ([]string, error)
	SetLimits(ctx context.Context, id string, l Limits) error
}

type Status struct {
	PID         int
	ActiveState string
	SubState    string
}

type Result struct {
	Code   int
	Signal string
}

type Limits struct {
	MemoryMaxBytes int64
	CPUQuotaPct    int
}
