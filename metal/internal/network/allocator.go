// Package network manages host virtual machine networks.
package network

import (
	"context"

	"github.com/frappe/atlas/metal/internal/vm"
)

// Allocator manages virtual machine network resources.
type Allocator interface {
	Allocate(ctx context.Context, request Request) (Interface, error)
	Resolve(virtualMachineID string) Interface
	Release(ctx context.Context, virtualMachineID string) error
}

// Request contains one virtual machine network request.
type Request struct {
	VirtualMachineID string
	Egress           vm.Egress
	PublicIPv4       string
	UserID           uint32
	GroupID          uint32
}

// Interface contains one virtual machine network interface.
type Interface struct {
	NetworkNamespacePath string
	TapName              string
	MACAddress           string
	GuestIPAddress       string
	GatewayIPAddress     string
}
