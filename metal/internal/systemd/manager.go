// Package systemd controls virtual machine services through D-Bus.
package systemd

import (
	"context"
	"syscall"
)

// Manager controls virtual machine systemd units.
type Manager interface {
	Start(ctx context.Context, id string) error
	Stop(ctx context.Context, id string) error
	Kill(ctx context.Context, id string, signal syscall.Signal) error
	ResetFailed(ctx context.Context, id string) error
	Status(ctx context.Context, id string) (Status, error)
	Wait(ctx context.Context, id string) (Result, error)
	List(ctx context.Context) ([]string, error)
	SetLimits(ctx context.Context, id string, limits Limits) error
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
	CPUQuotaPct    int
}
