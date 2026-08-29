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
