// Package vm defines Metal's hypervisor-agnostic VM abstraction.
package vm

import "context"

type VMDriver interface {
	Create(ctx context.Context, spec Spec) (VM, error)
	Load(ctx context.Context, id string) (VM, error)
	List(ctx context.Context) ([]VM, error)
	// Images lists the images VMs can be created from.
	Images(ctx context.Context) ([]Image, error)
	// DeleteImage removes an image. Returns ErrConflict if VMs cloned from it still
	// exist, ErrNotFound if it is unknown.
	DeleteImage(ctx context.Context, ref string) error
	Type() DriverType
}

type DriverType string

const (
	DriverFirecracker DriverType = "firecracker"
)
