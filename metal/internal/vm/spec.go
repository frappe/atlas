package vm

import (
	"maps"
	"slices"
	"strings"
)

// Spec contains the persistent configuration for one VM.
type Spec struct {
	VCPUs     int
	MemoryMiB int
	DiskMiB   int
	Disk      Disk
	Image     ImageRef
	Network   Network
	SSHKeys   []string
	Hostname  string
	UserData  string
	Metadata  map[string]string
}

// ImageRef identifies immutable boot files and their transport URLs.
type ImageRef struct {
	Name                        string
	RootfsURL                   string
	RootfsSHA256                string
	KernelURL                   string
	KernelSHA256                string
	Architecture                string
	CacheImage                  bool
	MemorySnapshot              bool
	MemorySnapshotConfiguration *MemorySnapshotConfiguration
}

// MemorySnapshotConfiguration is the exact shape of a local warm image.
type MemorySnapshotConfiguration struct {
	VirtualCPUCount int
	MemoryMiB       int
	DiskMiB         int
}

// Disk contains the requested VM disk limits. A zero value is unlimited.
type Disk struct {
	ThroughputMiBps int
	IOPS            int
}

// Network contains the requested VM network configuration.
type Network struct {
	PublicIPv4                    string
	WireGuardMeshIPv6             string
	PrivateNetworkThroughputMiBps int
	PublicNetworkThroughputMiBps  int
	Egress                        Egress
}

// SameReservation reports whether two specifications reserve the same VM.
func (spec Spec) SameReservation(other Spec) bool {
	return spec.VCPUs == other.VCPUs &&
		spec.MemoryMiB == other.MemoryMiB &&
		spec.DiskMiB == other.DiskMiB &&
		spec.Disk == other.Disk &&
		spec.Image.Name == other.Image.Name &&
		strings.EqualFold(spec.Image.RootfsSHA256, other.Image.RootfsSHA256) &&
		strings.EqualFold(spec.Image.KernelSHA256, other.Image.KernelSHA256) &&
		spec.Image.Architecture == other.Image.Architecture &&
		spec.Network == other.Network &&
		slices.Equal(spec.SSHKeys, other.SSHKeys) &&
		spec.Hostname == other.Hostname &&
		spec.UserData == other.UserData &&
		maps.Equal(spec.Metadata, other.Metadata)
}

// RefreshImageSource replaces expired image transport URLs.
func (spec Spec) RefreshImageSource(other Spec) Spec {
	spec.Image.RootfsURL = other.Image.RootfsURL
	spec.Image.KernelURL = other.Image.KernelURL
	spec.Image.CacheImage = other.Image.CacheImage
	spec.Image.MemorySnapshot = other.Image.MemorySnapshot
	spec.Image.MemorySnapshotConfiguration = other.Image.MemorySnapshotConfiguration
	return spec
}

// Egress controls internet reachability for a VM. It does not control mesh
// reachability. A VM keeps its private network attachment for every mode except
// EgressNone.
type Egress string

const (
	// EgressUplink routes VM traffic to the internet through the host uplink.
	EgressUplink Egress = "uplink"
	// EgressMesh keeps the private network attachment and gives no internet path.
	EgressMesh Egress = "mesh"
	// EgressNone removes the private network attachment and isolates the VM.
	EgressNone Egress = "none"
)

// IsValid reports whether the value names one egress mode.
func (egress Egress) IsValid() bool {
	switch egress {
	case EgressUplink, EgressMesh, EgressNone:
		return true
	default:
		return false
	}
}

// HasVirtualEthernet reports whether the mode keeps the private network attachment.
func (egress Egress) HasVirtualEthernet() bool {
	return egress == EgressUplink || egress == EgressMesh
}

// HasInternetPath reports whether the mode gives the VM a route to the internet.
func (egress Egress) HasInternetPath() bool {
	return egress == EgressUplink
}
