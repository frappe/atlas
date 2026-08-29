// Package vm defines Metal's hypervisor-agnostic VM abstraction.
package vm

import "context"

type VMDriver interface {
	Create(ctx context.Context, spec Spec) (VM, error)
	Load(ctx context.Context, id string) (VM, error)
	List(ctx context.Context) ([]VM, error)
	Type() DriverType
}

type DriverType string

const (
	DriverFirecracker DriverType = "firecracker"
)
