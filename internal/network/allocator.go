// Package network provisions per-VM networking (netns + TAP) from a logical ref.
package network

import "context"

type Allocator interface {
	// Allocate creates the netns + TAP for vmID and returns the resolved NIC.
	Allocate(ctx context.Context, vmID, ref string) (NIC, error)
	// Release tears down whatever Allocate created for vmID.
	Release(ctx context.Context, vmID string) error
}

type NIC struct {
	NetnsPath string // passed to jailer --netns
	TapName   string // firecracker network-interface host_dev_name
	MAC       string
	GuestIP   string
	GatewayIP string
}
