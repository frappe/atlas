// Package network provisions per-VM networking: each VM gets its own network
// namespace containing a TAP that firecracker attaches to.
package network

import "context"

type Allocator interface {
	// Allocate creates the netns + TAP for the VM and returns the resolved NIC.
	Allocate(ctx context.Context, req Request) (NIC, error)
	// Release tears down whatever Allocate created for vmID.
	Release(ctx context.Context, vmID string) error
}

type Request struct {
	VMID     string
	Ref      string // logical network ref (unused until multiple networks exist)
	UID, GID uint32 // TAP owner, so the jailed firecracker can attach to it
}

type NIC struct {
	NetnsPath string // passed to jailer --netns
	TapName   string // firecracker network-interface host_dev_name
	MAC       string
	GuestIP   string
	GatewayIP string
}
