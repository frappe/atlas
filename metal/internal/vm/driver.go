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
	ReplaceMetadata(ctx context.Context, id string, metadata map[string]string) error
	UpdateNetwork(ctx context.Context, id string, update NetworkUpdate) error
	ResizeCompute(ctx context.Context, id string, virtualCPUCount, memoryMiB int) error
	Reboot(ctx context.Context, id string) error
}

// NetworkUpdate contains mutable virtual machine network settings.
type NetworkUpdate struct {
	Egress                       Egress
	PublicIPv4                   string
	PrivateNetworkThroughputMbps int
	PublicNetworkThroughputMbps  int
}
