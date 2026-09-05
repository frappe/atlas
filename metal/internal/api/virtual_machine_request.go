package api

import (
	"fmt"
	"math"
	"net/netip"
	"net/url"
	"regexp"

	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	maxResourceIDLength = 64
	maximumMemoryMiB    = (math.MaxInt - 128) / 2
)

var (
	imageRefPattern     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
	resourceIDPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	sha256DigestPattern = regexp.MustCompile(`^[a-fA-F0-9]{64}$`)
	wireGuardMeshPrefix = netip.MustParsePrefix("fdaa::/16")
)

type createRequest struct {
	VCPUs     int               `json:"vcpus"`
	MemoryMiB int               `json:"memory_mib"`
	DiskMiB   int               `json:"disk_mib"`
	Disk      diskRequest       `json:"disk"`
	Image     imageRequest      `json:"image"`
	Network   networkRequest    `json:"network"`
	SSHKeys   []string          `json:"ssh_keys"`
	Hostname  string            `json:"hostname"`
	UserData  string            `json:"user_data"`
	Metadata  map[string]string `json:"metadata"`
}

type imageRequest struct {
	Ref                         string                              `json:"ref"`
	Architecture                string                              `json:"architecture"`
	Rootfs                      imageArtifactRequest                `json:"rootfs"`
	Kernel                      imageArtifactRequest                `json:"kernel"`
	CacheImage                  bool                                `json:"cache_image"`
	MemorySnapshot              bool                                `json:"memory_snapshot"`
	MemorySnapshotConfiguration *memorySnapshotConfigurationRequest `json:"memory_snapshot_configuration,omitempty"`
}

type memorySnapshotConfigurationRequest struct {
	VirtualCPUCount int `json:"virtual_cpu_count"`
	MemoryMiB       int `json:"memory_mib"`
	DiskMiB         int `json:"disk_mib"`
}

type imageArtifactRequest struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
}

type diskRequest struct {
	ThroughputMbps int `json:"throughput_mbps"`
	IOPS           int `json:"iops"`
}

func (request diskRequest) validate() error {
	if request.ThroughputMbps < 0 || request.IOPS < 0 {
		return fmt.Errorf("disk limits must not be negative")
	}
	return nil
}

func (request diskRequest) spec() vm.Disk {
	return vm.Disk{ThroughputMbps: request.ThroughputMbps, IOPS: request.IOPS}
}

type networkRequest struct {
	PublicIPv4                   string `json:"public_ipv4"`
	WireGuardMeshIPv6            string `json:"wireguard_mesh_ipv6"`
	PrivateNetworkThroughputMbps int    `json:"private_network_throughput_mbps"`
	PublicNetworkThroughputMbps  int    `json:"public_network_throughput_mbps"`
	Egress                       string `json:"egress"`
}

type computeResizeRequest struct {
	VCPUs     int `json:"vcpus"`
	MemoryMiB int `json:"memory_mib"`
}

type diskResizeRequest struct {
	DiskMiB int `json:"disk_mib"`
}

func (request createRequest) validate() error {
	if err := request.Disk.validate(); err != nil {
		return err
	}
	if request.VCPUs <= 0 || request.MemoryMiB <= 0 || request.DiskMiB <= 0 {
		return fmt.Errorf("vcpus, memory_mib, and disk_mib must be positive")
	}
	if request.MemoryMiB > maximumMemoryMiB {
		return fmt.Errorf("memory_mib is too large")
	}
	if err := request.Image.validate(); err != nil {
		return err
	}
	if err := request.Network.validate(); err != nil {
		return err
	}

	return validateMetadata(request.Metadata)
}

func (request createRequest) spec() vm.Spec {
	return vm.Spec{
		VCPUs:     request.VCPUs,
		MemoryMiB: request.MemoryMiB,
		DiskMiB:   request.DiskMiB,
		Disk:      request.Disk.spec(),
		Image:     request.Image.specification(),
		Network:   request.Network.spec(),
		SSHKeys:   request.SSHKeys,
		Hostname:  request.Hostname,
		UserData:  request.UserData,
		Metadata:  request.Metadata,
	}
}

func (request imageRequest) specification() vm.ImageRef {
	return vm.ImageRef{
		Name:                        request.Ref,
		Architecture:                request.Architecture,
		RootfsURL:                   request.Rootfs.URL,
		RootfsSHA256:                request.Rootfs.SHA256,
		KernelURL:                   request.Kernel.URL,
		KernelSHA256:                request.Kernel.SHA256,
		CacheImage:                  request.CacheImage,
		MemorySnapshot:              request.MemorySnapshot,
		MemorySnapshotConfiguration: request.MemorySnapshotConfiguration.specification(),
	}
}

func (request imageRequest) validate() error {
	if !validImageRef(request.Ref) {
		return fmt.Errorf("image.ref must match [A-Za-z0-9._:-] and start alphanumeric")
	}
	if request.Architecture == "" {
		return fmt.Errorf("image.architecture is required")
	}
	if !validHTTPURL(request.Rootfs.URL) || !validHTTPURL(request.Kernel.URL) {
		return fmt.Errorf("image rootfs and kernel URLs must use HTTP or HTTPS")
	}
	if !validSHA256Digest(request.Rootfs.SHA256) || !validSHA256Digest(request.Kernel.SHA256) {
		return fmt.Errorf("image rootfs and kernel SHA-256 values are invalid")
	}
	if request.MemorySnapshot {
		configuration := request.MemorySnapshotConfiguration
		if configuration == nil || configuration.VirtualCPUCount <= 0 || configuration.MemoryMiB <= 0 || configuration.DiskMiB <= 0 {
			return fmt.Errorf("image.memory_snapshot_configuration must contain positive values")
		}
	}

	return nil
}

func (request *memorySnapshotConfigurationRequest) specification() *vm.MemorySnapshotConfiguration {
	if request == nil {
		return nil
	}

	return &vm.MemorySnapshotConfiguration{
		VirtualCPUCount: request.VirtualCPUCount,
		MemoryMiB:       request.MemoryMiB,
		DiskMiB:         request.DiskMiB,
	}
}

func (request networkRequest) validate() error {
	if request.PrivateNetworkThroughputMbps < 0 || request.PublicNetworkThroughputMbps < 0 {
		return fmt.Errorf("network throughput values must not be negative")
	}

	wireGuardAddress, err := netip.ParseAddr(request.WireGuardMeshIPv6)
	if err != nil || !wireGuardMeshPrefix.Contains(wireGuardAddress) {
		return fmt.Errorf("network.wireguard_mesh_ipv6 must be in fdaa::/16")
	}
	egress := vm.Egress(request.Egress)
	if !egress.IsValid() {
		return fmt.Errorf("network.egress must be %s, %s, or %s", vm.EgressUplink, vm.EgressMesh, vm.EgressNone)
	}
	if request.PublicIPv4 == "" {
		return nil
	}

	publicAddress, err := netip.ParseAddr(request.PublicIPv4)
	if err != nil || !publicAddress.Is4() {
		return fmt.Errorf("network.public_ipv4 must be an IPv4 address")
	}
	if !egress.HasInternetPath() {
		return fmt.Errorf("network.public_ipv4 requires %s egress", vm.EgressUplink)
	}
	return nil
}

func (request networkRequest) spec() vm.Network {
	return vm.Network{
		PublicIPv4:                   request.PublicIPv4,
		WireGuardMeshIPv6:            request.WireGuardMeshIPv6,
		PrivateNetworkThroughputMbps: request.PrivateNetworkThroughputMbps,
		PublicNetworkThroughputMbps:  request.PublicNetworkThroughputMbps,
		Egress:                       vm.Egress(request.Egress),
	}
}

func validHTTPURL(value string) bool {
	parsed, err := url.ParseRequestURI(value)
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Host != ""
}

func validImageRef(value string) bool {
	return imageRefPattern.MatchString(value)
}

func validResourceID(value string) bool {
	return len(value) <= maxResourceIDLength && resourceIDPattern.MatchString(value)
}

func validSHA256Digest(value string) bool {
	return sha256DigestPattern.MatchString(value)
}
