package api

type MachineConfig struct {
	VCPUCount  int  `json:"vcpu_count"`
	MemSizeMiB int  `json:"mem_size_mib"`
	SMT        bool `json:"smt,omitempty"`
}

type BootSource struct {
	KernelImagePath string `json:"kernel_image_path"`
	BootArgs        string `json:"boot_args,omitempty"`
	InitrdPath      string `json:"initrd_path,omitempty"`
}

type Drive struct {
	DriveID      string `json:"drive_id"`
	PathOnHost   string `json:"path_on_host"`
	IsRootDevice bool   `json:"is_root_device"`
	IsReadOnly   bool   `json:"is_read_only"`
}

// PartialDrive is the PATCH /drives/{id} body. Re-sending the same path_on_host
// makes firecracker re-read the backing device's size (a rescan). Firecracker's
// PartialDrive schema only accepts drive_id + path_on_host here.
type PartialDrive struct {
	DriveID    string `json:"drive_id"`
	PathOnHost string `json:"path_on_host"`
}

type NetworkInterface struct {
	IfaceID     string `json:"iface_id"`
	HostDevName string `json:"host_dev_name"`
	GuestMAC    string `json:"guest_mac,omitempty"`
}

type action struct {
	ActionType string `json:"action_type"`
}

// MmdsConfig enables the microVM metadata service on the given interfaces.
type MmdsConfig struct {
	NetworkInterfaces []string `json:"network_interfaces"`
	Version           string   `json:"version,omitempty"`      // "V1" or "V2"
	IPv4Address       string   `json:"ipv4_address,omitempty"` // link-local, e.g. 169.254.169.254
}

// InstanceInfo is firecracker's runtime state, from GET "/".
type InstanceInfo struct {
	ID    string `json:"id"`
	State string `json:"state"` // "Not started", "Running", "Paused"
}

// vmState is the PATCH /vm body that changes the run state.
type vmState struct {
	State string `json:"state"` // "Paused" or "Resumed"
}

// CreateSnapshotReq is the PUT /snapshot/create body. The paths are relative to
// the jailed process's chroot root and are written as the VM's uid.
type CreateSnapshotReq struct {
	SnapshotType string `json:"snapshot_type"` // "Full"
	SnapshotPath string `json:"snapshot_path"` // device + vCPU state file
	MemFilePath  string `json:"mem_file_path"` // guest memory file
	// SyncFiles fsyncs the snapshot files before create returns. nil keeps
	// firecracker's default (true); false trades crash-safety for a shorter pause.
	SyncFiles *bool `json:"sync_snapshot_files,omitempty"`
}

// MemBackend tells firecracker how to read a snapshot's guest memory on load.
type MemBackend struct {
	BackendPath string `json:"backend_path"` // memory file (File) or socket (Uffd), chroot-relative
	BackendType string `json:"backend_type"` // "File" or "Uffd"
}

// LoadSnapshotReq is the PUT /snapshot/load body. The block device and TAP named
// in the snapshot must already exist at the same paths before the call.
type LoadSnapshotReq struct {
	SnapshotPath string     `json:"snapshot_path"`
	MemBackend   MemBackend `json:"mem_backend"`
	ResumeVM     bool       `json:"resume_vm"`
}
