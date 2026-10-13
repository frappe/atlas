package vm

import "context"

// NetworkInterface contains the host network values used by a runtime.
type NetworkInterface struct {
	NetworkNamespacePath string `json:"network_namespace_path"`
	TapName              string `json:"tap_name"`
	MACAddress           string `json:"mac_address"`
	GuestIPAddress       string `json:"guest_ip_address"`
	GatewayIPAddress     string `json:"gateway_ip_address"`
}

// Network converges and releases host network resources.
type Network interface {
	Ensure(context.Context, NetworkRequest) (NetworkInterface, error)
	Release(context.Context, NetworkReleaseRequest) error
}

// NetworkRequest contains the complete desired host network state.
type NetworkRequest struct {
	VirtualMachineID                string
	UserID                          uint32
	GroupID                         uint32
	Configuration                   NetworkConfiguration
	TrackTraffic                    bool
	FailOnTrafficMonitorAttachError bool
}

// NetworkReleaseRequest identifies host network resources to remove.
type NetworkReleaseRequest struct {
	VirtualMachineID  string
	UserID            uint32
	WireGuardMeshIPv6 string
}
