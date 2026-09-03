// Package vm defines virtual machine contracts.
package vm

import "context"

// Driver manages virtual machine reservations.
type Driver interface {
	Create(ctx context.Context, id string, specification Spec) (VM, error)
	Load(ctx context.Context, id string) (VM, error)
	List(ctx context.Context) ([]VM, error)
	SetDesiredState(ctx context.Context, id string, state State) error
	ReplaceSSHKeys(ctx context.Context, id string, sshKeys []string) error
	ResizeCompute(ctx context.Context, id string, virtualCPUCount, memoryMiB int) error
}
