package api

import (
	"maps"

	"github.com/frappe/atlas/metal/internal/vm"
)

// virtualMachineResponse pairs desired and observed VM state. Their generations
// tell the controller when a change reached the host.
type virtualMachineResponse struct {
	ID       string                         `json:"id"`
	Desired  desiredVirtualMachineResponse  `json:"desired"`
	Observed observedVirtualMachineResponse `json:"observed"`
}

// desiredVirtualMachineResponse is the stored controller intent.
type desiredVirtualMachineResponse struct {
	Generation        uint64                      `json:"generation"`
	RestartGeneration uint64                      `json:"restart_generation"`
	State             string                      `json:"state"`
	Compute           computeResponse             `json:"compute"`
	Disk              diskResponse                `json:"disk"`
	Image             virtualMachineImageResponse `json:"image"`
	Network           networkResponse             `json:"network"`
	Guest             guestResponse               `json:"guest"`
}

// observedVirtualMachineResponse is the state reached by the host and any
// operation currently in progress.
type observedVirtualMachineResponse struct {
	Generation        uint64                  `json:"generation"`
	RestartGeneration uint64                  `json:"restart_generation"`
	State             string                  `json:"state"`
	Phase             string                  `json:"phase,omitempty"`
	OperationID       string                  `json:"operation_id,omitempty"`
	OperationStarted  string                  `json:"operation_started_at,omitempty"`
	UpdatedAt         string                  `json:"updated_at"`
	Disk              observedDiskResponse    `json:"disk"`
	Network           observedNetworkResponse `json:"network"`
	Error             *operationErrorResponse `json:"error"`
}

// computeResponse is the requested compute configuration.
type computeResponse struct {
	CPUMillicores         int `json:"cpu_millicores"`
	MemoryMiB             int `json:"memory_mib"`
	SleepAfterIdleSeconds int `json:"sleep_after_idle_seconds"`
}

// guestResponse is the guest-facing configuration. User data is not returned.
type guestResponse struct {
	Hostname string            `json:"hostname"`
	SSHKeys  []string          `json:"ssh_keys"`
	Metadata map[string]string `json:"metadata"`
}

// virtualMachineImageResponse identifies boot content without signed,
// short-lived transport URLs.
type virtualMachineImageResponse struct {
	Ref                         string                               `json:"ref"`
	Architecture                string                               `json:"architecture"`
	Rootfs                      imageArtifactResponse                `json:"rootfs"`
	Kernel                      imageArtifactResponse                `json:"kernel"`
	CacheImage                  bool                                 `json:"cache_image"`
	MemorySnapshot              bool                                 `json:"memory_snapshot"`
	MemorySnapshotConfiguration *memorySnapshotConfigurationResponse `json:"memory_snapshot_configuration,omitempty"`
}

// memorySnapshotConfigurationResponse is the VM shape a warm image serves.
type memorySnapshotConfigurationResponse struct {
	VirtualCPUCount int `json:"virtual_cpu_count"`
	MemoryMiB       int `json:"memory_mib"`
	DiskMiB         int `json:"disk_mib"`
}

// imageArtifactResponse is one artifact digest.
type imageArtifactResponse struct {
	SHA256 string `json:"sha256"`
}

// networkResponse is the desired VM network.
type networkResponse struct {
	PublicIPv4                    string           `json:"public_ipv4,omitempty"`
	Routes                        []routeResponse  `json:"routes"`
	IsNetworkGateway              bool             `json:"is_network_gateway,omitempty"`
	PublicIPv6                    string           `json:"public_ipv6,omitempty"`
	WireGuardMeshIPv6             string           `json:"wireguard_mesh_ipv6"`
	PrivateNetworkThroughputMiBps int              `json:"private_network_throughput_mibps"`
	PublicNetworkThroughputMiBps  int              `json:"public_network_throughput_mibps"`
	Firewall                      firewallResponse `json:"firewall"`
}

type routeResponse struct {
	Destination string `json:"destination"`
	Via         string `json:"via"`
	Scope       string `json:"scope,omitempty"`
}

func toRoutes(routes []vm.Route) []routeResponse {
	responses := make([]routeResponse, len(routes))
	for index, route := range routes {
		responses[index] = routeResponse{
			Destination: route.Destination,
			Via:         route.Via,
			Scope:       route.Scope,
		}
	}
	return responses
}

// firewallResponse is the complete desired firewall configuration.
type firewallResponse struct {
	Enabled  bool                   `json:"enabled"`
	Inbound  []firewallRuleResponse `json:"inbound"`
	Outbound []firewallRuleResponse `json:"outbound"`
}

// firewallRuleResponse permits traffic from or to a set of IP prefixes.
type firewallRuleResponse struct {
	Protocol string   `json:"protocol"`
	Ports    string   `json:"ports,omitempty"`
	CIDRs    []string `json:"cidrs"`
}

// diskResponse is the desired disk size and rate limits.
type diskResponse struct {
	ThroughputMiBps int `json:"throughput_mibps"`
	IOPS            int `json:"iops"`
	SizeMiB         int `json:"size_mib"`
}

// observedDiskResponse is the disk the host actually provides.
type observedDiskResponse struct {
	UsedMiB int `json:"used_mib"`
}

// observedNetworkResponse is the network identity the host assigned.
type observedNetworkResponse struct {
	MAC string `json:"mac,omitempty"`
}

// operationErrorResponse contains the public part of a reconciliation failure;
// local detail stays on the host.
type operationErrorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	UpdatedAt string `json:"updated_at"`
}

// toVirtualMachine converts VM information into the response form.
func toVirtualMachine(information vm.Information) virtualMachineResponse {
	return virtualMachineResponse{
		ID: information.ID,
		Desired: desiredVirtualMachineResponse{
			Generation:        information.DesiredGeneration,
			RestartGeneration: information.DesiredRestartGeneration,
			State:             string(information.DesiredState),
			Compute: computeResponse{
				CPUMillicores:         information.CPUMillicores,
				MemoryMiB:             information.MemoryMiB,
				SleepAfterIdleSeconds: information.SleepAfterIdleSeconds,
			},
			Disk: diskResponse{
				ThroughputMiBps: information.DiskThroughputMiBps,
				IOPS:            information.DiskIOPS,
				SizeMiB:         information.DiskMiB,
			},
			Image: toVirtualMachineImage(information.Image),
			Network: networkResponse{
				PublicIPv4:                    information.PublicIPv4,
				Routes:                        toRoutes(information.Routes),
				IsNetworkGateway:              information.IsNetworkGateway,
				PublicIPv6:                    information.PublicIPv6,
				WireGuardMeshIPv6:             information.WireGuardMeshIPv6,
				PrivateNetworkThroughputMiBps: information.PrivateNetworkThroughputMiBps,
				PublicNetworkThroughputMiBps:  information.PublicNetworkThroughputMiBps,
				Firewall:                      toFirewall(information.Firewall),
			},
			Guest: guestResponse{
				Hostname: information.Hostname,
				SSHKeys:  append([]string{}, information.SSHKeys...),
				Metadata: cloneMetadata(information.Metadata),
			},
		},
		Observed: observedVirtualMachineResponse{
			Generation:        information.ObservedGeneration,
			RestartGeneration: information.ObservedRestartGeneration,
			State:             string(information.State),
			Phase:             information.Phase,
			OperationID:       information.OperationID,
			OperationStarted:  formatRFC3339(information.OperationStartedAt),
			UpdatedAt:         formatRFC3339(information.UpdatedAt),
			Disk:              observedDiskResponse{UsedMiB: information.DiskUsedMiB},
			Network:           observedNetworkResponse{MAC: information.MAC},
			Error:             toOperationError(information.Error),
		},
	}
}

func toFirewall(configuration vm.FirewallConfiguration) firewallResponse {
	return firewallResponse{
		Enabled:  configuration.Enabled,
		Inbound:  toFirewallRules(configuration.Inbound),
		Outbound: toFirewallRules(configuration.Outbound),
	}
}

func toFirewallRules(rules []vm.FirewallRule) []firewallRuleResponse {
	responses := make([]firewallRuleResponse, len(rules))
	for index, rule := range rules {
		responses[index] = firewallRuleResponse{
			Protocol: string(rule.Protocol),
			Ports:    rule.Ports,
			CIDRs:    append([]string{}, rule.CIDRs...),
		}
	}
	return responses
}

// cloneMetadata copies the map, so a response cannot alias stored state.
func cloneMetadata(metadata map[string]string) map[string]string {
	if metadata == nil {
		return map[string]string{}
	}
	return maps.Clone(metadata)
}

// toOperationError converts a reconciliation failure into its safe form.
func toOperationError(operationError *vm.PublicOperationError) *operationErrorResponse {
	if operationError == nil {
		return nil
	}
	return &operationErrorResponse{
		Code:      operationError.Code,
		Message:   operationError.Message,
		UpdatedAt: formatRFC3339(operationError.UpdatedAt),
	}
}

// toVirtualMachineImage converts an image into the response form.
func toVirtualMachineImage(image vm.Image) virtualMachineImageResponse {
	return virtualMachineImageResponse{
		Ref:                         image.Name,
		Architecture:                image.Architecture,
		Rootfs:                      imageArtifactResponse{SHA256: image.RootfsSHA256},
		Kernel:                      imageArtifactResponse{SHA256: image.KernelSHA256},
		CacheImage:                  image.CacheImage,
		MemorySnapshot:              image.MemorySnapshot,
		MemorySnapshotConfiguration: toMemorySnapshotConfiguration(image.MemorySnapshotConfiguration),
	}
}

// toMemorySnapshotConfiguration converts a warm image shape to its response form.
func toMemorySnapshotConfiguration(configuration *vm.MemorySnapshotConfiguration) *memorySnapshotConfigurationResponse {
	if configuration == nil {
		return nil
	}

	return &memorySnapshotConfigurationResponse{
		VirtualCPUCount: configuration.VirtualCPUCount,
		MemoryMiB:       configuration.MemoryMiB,
		DiskMiB:         configuration.DiskMiB,
	}
}
