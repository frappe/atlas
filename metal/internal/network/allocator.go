// Package network manages host virtual machine networks.
package network

import (
	"context"

	"github.com/frappe/atlas/metal/internal/vm"
)

// Allocator manages virtual machine network resources.
type Allocator interface {
	Allocate(ctx context.Context, request Request) (Interface, error)
	Update(ctx context.Context, request UpdateRequest) error
	Resolve(virtualMachineID string) Interface
	Release(ctx context.Context, request ReleaseRequest) error
}

// ReleaseRequest identifies the virtual machine network to remove.
type ReleaseRequest struct {
	VirtualMachineID  string
	UserID            uint32
	WireGuardMeshIPv6 string
}

// UpdateRequest contains one live virtual machine network update.
type UpdateRequest struct {
	VirtualMachineID string
	UserID           uint32
	Previous         vm.Network
	Desired          vm.Network
}

// Request contains one virtual machine network request.
type Request struct {
	VirtualMachineID              string
	Egress                        vm.Egress
	PublicIPv4                    string
	WireGuardMeshIPv6             string
	PrivateNetworkThroughputMiBps int
	PublicNetworkThroughputMiBps  int
	UserID                        uint32
	GroupID                       uint32
}

// release returns the request that removes this allocation.
func (request Request) release() ReleaseRequest {
	return ReleaseRequest{
		VirtualMachineID:  request.VirtualMachineID,
		UserID:            request.UserID,
		WireGuardMeshIPv6: request.WireGuardMeshIPv6,
	}
}

func (request Request) trafficControl() trafficControlRequest {
	return trafficControlRequest{
		VirtualMachineID:              request.VirtualMachineID,
		UserID:                        request.UserID,
		Egress:                        request.Egress,
		PrivateNetworkThroughputMiBps: request.PrivateNetworkThroughputMiBps,
		PublicNetworkThroughputMiBps:  request.PublicNetworkThroughputMiBps,
	}
}

// Interface contains one virtual machine network interface.
type Interface struct {
	NetworkNamespacePath string
	TapName              string
	MACAddress           string
	GuestIPAddress       string
	GatewayIPAddress     string
}
