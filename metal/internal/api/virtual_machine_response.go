package api

import (
	"maps"

	"github.com/frappe/atlas/metal/internal/vm"
)

type virtualMachineResponse struct {
	ID           string                      `json:"id"`
	State        string                      `json:"state"`
	DesiredState string                      `json:"desired_state"`
	Error        string                      `json:"error,omitempty"`
	VCPUs        int                         `json:"vcpus"`
	MemoryMiB    int                         `json:"memory_mib"`
	Image        virtualMachineImageResponse `json:"image"`
	SSHKeys      []string                    `json:"ssh_keys"`
	Hostname     string                      `json:"hostname"`
	Metadata     map[string]string           `json:"metadata,omitempty"`
	Network      networkResponse             `json:"network"`
	Disk         diskResponse                `json:"disk"`
}

type virtualMachineImageResponse struct {
	Ref                         string                               `json:"ref"`
	Architecture                string                               `json:"architecture"`
	Rootfs                      imageArtifactResponse                `json:"rootfs"`
	Kernel                      imageArtifactResponse                `json:"kernel"`
	CacheImage                  bool                                 `json:"cache_image"`
	MemorySnapshot              bool                                 `json:"memory_snapshot"`
	MemorySnapshotConfiguration *memorySnapshotConfigurationResponse `json:"memory_snapshot_configuration,omitempty"`
}

type memorySnapshotConfigurationResponse struct {
	VirtualCPUCount int `json:"virtual_cpu_count"`
	MemoryMiB       int `json:"memory_mib"`
	DiskMiB         int `json:"disk_mib"`
}

type imageArtifactResponse struct {
	SHA256 string `json:"sha256"`
}

type networkResponse struct {
	MAC               string `json:"mac"`
	PublicIPv4        string `json:"public_ipv4,omitempty"`
	WireGuardMeshIPv6 string `json:"wireguard_mesh_ipv6"`
	Egress            string `json:"egress"`
}

type diskResponse struct {
	SizeMiB int `json:"size_mib"`
	UsedMiB int `json:"used_mib"`
}

type virtualMachineListResponse struct {
	VMs []virtualMachineResponse `json:"vms"`
}

func toVirtualMachine(information vm.Info) virtualMachineResponse {
	return virtualMachineResponse{
		ID:           information.ID,
		State:        string(information.State),
		DesiredState: string(information.DesiredState),
		Error:        information.Error,
		VCPUs:        information.VCPUs,
		MemoryMiB:    information.MemoryMiB,
		Image:        toVirtualMachineImage(information.Image),
		SSHKeys:      append([]string(nil), information.SSHKeys...),
		Hostname:     information.Hostname,
		Metadata:     maps.Clone(information.Metadata),
		Network: networkResponse{
			MAC:               information.MAC,
			PublicIPv4:        information.PublicIPv4,
			WireGuardMeshIPv6: information.WireGuardMeshIPv6,
			Egress:            string(information.Egress),
		},
		Disk: diskResponse{
			SizeMiB: information.DiskMiB,
			UsedMiB: information.DiskUsedMiB,
		},
	}
}

func toVirtualMachineImage(image vm.ImageRef) virtualMachineImageResponse {
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
