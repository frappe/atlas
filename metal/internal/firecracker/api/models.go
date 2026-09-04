package api

// MachineConfig contains Firecracker compute settings.
type MachineConfig struct {
	VCPUCount  int  `json:"vcpu_count"`
	MemSizeMiB int  `json:"mem_size_mib"`
	SMT        bool `json:"smt,omitempty"`
}

// BootSource contains the kernel and boot arguments.
type BootSource struct {
	KernelImagePath string `json:"kernel_image_path"`
	BootArgs        string `json:"boot_args,omitempty"`
	InitrdPath      string `json:"initrd_path,omitempty"`
}

// Drive contains one Firecracker drive.
type Drive struct {
	DriveID      string `json:"drive_id"`
	PathOnHost   string `json:"path_on_host"`
	IsRootDevice bool   `json:"is_root_device"`
	IsReadOnly   bool   `json:"is_read_only"`
}

// PartialDrive contains the fields that rescan a drive.
type PartialDrive struct {
	DriveID    string `json:"drive_id"`
	PathOnHost string `json:"path_on_host"`
}

// NetworkInterface contains one Firecracker network interface.
type NetworkInterface struct {
	IfaceID     string `json:"iface_id"`
	HostDevName string `json:"host_dev_name"`
	GuestMAC    string `json:"guest_mac,omitempty"`
}

type action struct {
	ActionType string `json:"action_type"`
}

// MMDSConfig configures the metadata service.
type MMDSConfig struct {
	NetworkInterfaces []string `json:"network_interfaces"`
	Version           string   `json:"version,omitempty"`
	IPv4Address       string   `json:"ipv4_address,omitempty"`
	IMDSCompat        bool     `json:"imds_compat,omitempty"`
}

// InstanceInfo contains the Firecracker process state.
type InstanceInfo struct {
	ID    string `json:"id"`
	State string `json:"state"`
}

type virtualMachineState struct {
	State string `json:"state"`
}

// CreateSnapshotRequest contains paths for a new snapshot.
type CreateSnapshotRequest struct {
	SnapshotType string `json:"snapshot_type"`
	SnapshotPath string `json:"snapshot_path"`
	MemoryFile   string `json:"mem_file_path"`
	SyncFiles    *bool  `json:"sync_snapshot_files,omitempty"`
}

// MemoryBackend identifies the memory file for snapshot restore.
type MemoryBackend struct {
	Path string `json:"backend_path"`
	Type string `json:"backend_type"`
}

// LoadSnapshotRequest contains files for snapshot restore.
type LoadSnapshotRequest struct {
	SnapshotPath string        `json:"snapshot_path"`
	Memory       MemoryBackend `json:"mem_backend"`
	Resume       bool          `json:"resume_vm"`
}
