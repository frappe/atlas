package vm

import "context"

// NetworkInterface contains the host network values used by a runtime.
type NetworkInterface struct {
	NetworkNamespacePath string
	TapName              string
	MACAddress           string
	GuestIPAddress       string
	GatewayIPAddress     string
}

// Network converges and releases host network resources.
type Network interface {
	Ensure(context.Context, NetworkRequest) (NetworkInterface, error)
	Release(context.Context, NetworkReleaseRequest) error
}

// NetworkRequest contains the complete desired host network state.
type NetworkRequest struct {
	VirtualMachineID string
	UserID           uint32
	GroupID          uint32
	Configuration    NetworkConfiguration
	TrackTraffic     bool
	// RequireTrafficMonitor fails the request when traffic monitoring cannot
	// attach. Idle-shutdown needs this; metrics collection alone does not.
	RequireTrafficMonitor bool
}

// NetworkReleaseRequest identifies host network resources to remove.
type NetworkReleaseRequest struct {
	VirtualMachineID  string
	UserID            uint32
	WireGuardMeshIPv6 string
}
