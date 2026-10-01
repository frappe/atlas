package vm

import (
	"maps"
	"slices"
	"strings"
	"time"
)

// Specification contains the persistent configuration for one VM.
type Specification struct {
	CPUMillicores         int                  `json:"cpu_millicores"`
	MemoryMiB             int                  `json:"memory_mib"`
	SleepAfterIdleSeconds int                  `json:"sleep_after_idle_seconds,omitempty"`
	DiskMiB               int                  `json:"disk_mib"`
	Disk                  Disk                 `json:"disk"`
	Image                 Image                `json:"image"`
	Network               NetworkConfiguration `json:"network"`
	SSHKeys               []string             `json:"ssh_keys"`
	Hostname              string               `json:"hostname"`
	UserData              string               `json:"user_data"`
	Metadata              map[string]string    `json:"metadata"`
}

// Compute is the requested compute configuration of one VM.
type Compute struct {
	CPUMillicores         int
	MemoryMiB             int
	SleepAfterIdleSeconds int
}

// Image identifies immutable boot files and their transport URLs.
type Image struct {
	Name                        string                       `json:"name"`
	RootfsURL                   string                       `json:"rootfs_url"`
	RootfsSHA256                string                       `json:"rootfs_sha256"`
	KernelURL                   string                       `json:"kernel_url"`
	KernelSHA256                string                       `json:"kernel_sha256"`
	Architecture                string                       `json:"architecture"`
	CacheImage                  bool                         `json:"cache_image"`
	MemorySnapshot              bool                         `json:"memory_snapshot"`
	MemorySnapshotConfiguration *MemorySnapshotConfiguration `json:"memory_snapshot_configuration,omitempty"`
}

// MemorySnapshotConfiguration is the exact shape of a local warm image.
type MemorySnapshotConfiguration struct {
	VirtualCPUCount int `json:"virtual_cpu_count"`
	MemoryMiB       int `json:"memory_mib"`
	DiskMiB         int `json:"disk_mib"`
}

// Disk contains the requested VM disk limits. A zero value is unlimited.
type Disk struct {
	ThroughputMiBps int `json:"throughput_mibps"`
	IOPS            int `json:"iops"`
}

// RouteViaHost sends a destination through the Metal host uplink.
const RouteViaHost = "host"

// RouteScopeWireGuardGateway marks a return route for WireGuard gateway
// clients. A scoped route lives only in the VM namespace, so the guest
// metadata never lists it and the guest routing table keeps no copy.
const RouteScopeWireGuardGateway = "wireguard-gateway"

// Route sends one destination range through the host or through a gateway VM.
// Via is RouteViaHost or the WG Mesh address of a gateway VM. Scope is empty
// for a guest route or RouteScopeWireGuardGateway for a namespace-only route.
type Route struct {
	Destination string `json:"destination"`
	Via         string `json:"via"`
	Scope       string `json:"scope,omitempty"`
}

// IsViaHost reports whether the host uplink carries the route.
func (route Route) IsViaHost() bool {
	return route.Via == RouteViaHost
}

// IsWireGuardGateway reports whether the route returns WireGuard gateway
// client traffic through a gateway VM and stays out of the guest.
func (route Route) IsWireGuardGateway() bool {
	return route.Scope == RouteScopeWireGuardGateway
}

// IsIPv4 reports whether the route destination is an IPv4 prefix.
func (route Route) IsIPv4() bool {
	return !strings.Contains(route.Destination, ":")
}

// NetworkConfiguration contains the requested VM network configuration.
type NetworkConfiguration struct {
	PublicIPv4        string `json:"public_ipv4"`
	WireGuardMeshIPv6 string `json:"wireguard_mesh_ipv6"`
	// Routes send each destination range through the host or a gateway VM. A VM without routes reaches only the mesh.
	// A route with the WireGuard gateway scope stays in the VM namespace instead.
	Routes []Route `json:"routes,omitempty"`
	// IsNetworkGateway lets this VM send a source address it does not own, so it can carry traffic for other VMs.
	IsNetworkGateway bool `json:"is_network_gateway,omitempty"`
	// PublicIPv6 is a public address or block. The host maps a /128 to the mesh address and routes a larger block into the VM.
	PublicIPv6                    string                `json:"public_ipv6,omitempty"`
	PrivateNetworkThroughputMiBps int                   `json:"private_network_throughput_mibps"`
	PublicNetworkThroughputMiBps  int                   `json:"public_network_throughput_mibps"`
	Firewall                      FirewallConfiguration `json:"firewall"`
}

// Equal reports whether two network configurations contain the same desired values.
func (configuration NetworkConfiguration) Equal(other NetworkConfiguration) bool {
	return configuration.PublicIPv4 == other.PublicIPv4 &&
		slices.Equal(configuration.Routes, other.Routes) &&
		configuration.IsNetworkGateway == other.IsNetworkGateway &&
		configuration.PublicIPv6 == other.PublicIPv6 &&
		configuration.WireGuardMeshIPv6 == other.WireGuardMeshIPv6 &&
		configuration.PrivateNetworkThroughputMiBps == other.PrivateNetworkThroughputMiBps &&
		configuration.PublicNetworkThroughputMiBps == other.PublicNetworkThroughputMiBps &&
		configuration.Firewall.Equal(other.Firewall)
}

// HasNetworkAttachment reports whether the VM has a mesh network attachment.
func (configuration NetworkConfiguration) HasNetworkAttachment() bool {
	return configuration.WireGuardMeshIPv6 != ""
}

// HasIPv4HostRoute reports whether the host carries an IPv4 route.
func (configuration NetworkConfiguration) HasIPv4HostRoute() bool {
	return slices.ContainsFunc(configuration.Routes, func(route Route) bool { return route.IsIPv4() && route.IsViaHost() })
}

// HasIPv6HostRoute reports whether the host carries an IPv6 route.
func (configuration NetworkConfiguration) HasIPv6HostRoute() bool {
	return slices.ContainsFunc(configuration.Routes, func(route Route) bool { return !route.IsIPv4() && route.IsViaHost() })
}

// HasPublicIPv6Address reports whether PublicIPv6 is one mapped address.
func (configuration NetworkConfiguration) HasPublicIPv6Address() bool {
	return strings.HasSuffix(configuration.PublicIPv6, "/128")
}

// RoutesViaGateway returns routes that a gateway VM carries.
func (configuration NetworkConfiguration) RoutesViaGateway() []Route {
	var routes []Route
	for _, route := range configuration.Routes {
		if !route.IsViaHost() {
			routes = append(routes, route)
		}
	}
	return routes
}

// SameReservation reports whether two specifications reserve the same VM. It
// ignores signed image URLs, which can rotate without changing the reservation.
func (specification Specification) SameReservation(other Specification) bool {
	return specification.CPUMillicores == other.CPUMillicores &&
		specification.MemoryMiB == other.MemoryMiB &&
		specification.SleepAfterIdleSeconds == other.SleepAfterIdleSeconds &&
		specification.DiskMiB == other.DiskMiB &&
		specification.Disk == other.Disk &&
		specification.Image.Name == other.Image.Name &&
		strings.EqualFold(specification.Image.RootfsSHA256, other.Image.RootfsSHA256) &&
		strings.EqualFold(specification.Image.KernelSHA256, other.Image.KernelSHA256) &&
		specification.Image.Architecture == other.Image.Architecture &&
		specification.Network.Equal(other.Network) &&
		slices.Equal(specification.SSHKeys, other.SSHKeys) &&
		specification.Hostname == other.Hostname &&
		specification.UserData == other.UserData &&
		maps.Equal(specification.Metadata, other.Metadata)
}

// VirtualCPUCount returns the integer CPU count that Firecracker exposes to the guest.
func (specification Specification) VirtualCPUCount() int {
	count := specification.CPUMillicores / 1000
	if specification.CPUMillicores%1000 != 0 {
		count++
	}
	return count
}

// RefreshImageSource replaces image URLs and caching intent so a retry can use
// fresh signed URLs without changing the reservation.
func (specification Specification) RefreshImageSource(other Specification) Specification {
	specification.Image.RootfsURL = other.Image.RootfsURL
	specification.Image.KernelURL = other.Image.KernelURL
	specification.Image.CacheImage = other.Image.CacheImage
	specification.Image.MemorySnapshot = other.Image.MemorySnapshot
	specification.Image.MemorySnapshotConfiguration = other.Image.MemorySnapshotConfiguration
	return specification
}

// State is a virtual machine condition.
type State string

const (
	// StateUnknown means the runtime has not been inspected yet.
	StateUnknown State = "unknown"
	// StateCreated means the guest exists but has not started.
	StateCreated State = "created"
	// StateRunning means the guest is executing.
	StateRunning State = "running"
	// StatePaused means the guest is resident but not executing.
	StatePaused State = "paused"
	// StateStopped means the guest is not running and can start again.
	StateStopped State = "stopped"
	// StateFailed means the runtime stopped the guest unexpectedly.
	StateFailed State = "failed"
	// StateDestroyed means every host resource is released.
	StateDestroyed State = "destroyed"
)

// IsDesiredState reports whether state is a valid requested state.
func IsDesiredState(state State) bool {
	switch state {
	case StateRunning, StateStopped, StatePaused, StateDestroyed:
		return true
	default:
		return false
	}
}

// isObservedState reports whether state is one a runtime can report.
func isObservedState(state State) bool {
	switch state {
	case StateUnknown, StateCreated, StateRunning, StatePaused, StateStopped, StateFailed, StateDestroyed:
		return true
	default:
		return false
	}
}

// Information describes a virtual machine.
type Information struct {
	ID                            string
	State                         State
	DesiredState                  State
	Error                         *PublicOperationError
	CPUMillicores                 int
	MemoryMiB                     int
	DiskMiB                       int
	DiskUsedMiB                   int
	DiskThroughputMiBps           int
	DiskIOPS                      int
	Image                         Image
	SSHKeys                       []string
	Hostname                      string
	Metadata                      map[string]string
	SleepAfterIdleSeconds         int
	MAC                           string
	PublicIPv4                    string
	WireGuardMeshIPv6             string
	Routes                        []Route
	IsNetworkGateway              bool
	PublicIPv6                    string
	PrivateNetworkThroughputMiBps int
	PublicNetworkThroughputMiBps  int
	Firewall                      FirewallConfiguration
	DesiredGeneration             uint64
	DesiredRestartGeneration      uint64
	ObservedGeneration            uint64
	ObservedRestartGeneration     uint64
	Phase                         string
	OperationID                   string
	OperationStartedAt            time.Time
	UpdatedAt                     time.Time
}

// PublicOperationError contains safe reconciliation error data.
type PublicOperationError struct {
	Code      string
	Message   string
	UpdatedAt time.Time
}

// HostReachedDestinations lists the IPv6 destinations that a guest and its
// namespace send to the host. The host uplink or a gateway VM then carries them.
// A scoped route stays in the VM namespace, so the guest never installs it.
func HostReachedDestinations(network NetworkConfiguration) []string {
	var destinations []string
	for _, route := range network.Routes {
		if !route.IsIPv4() && !route.IsWireGuardGateway() {
			destinations = append(destinations, route.Destination)
		}
	}
	return destinations
}
