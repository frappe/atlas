package vm

type Spec struct {
	VCPUs   int
	MemMiB  int
	DiskMiB int
	Image   ImageRef
	Network NetworkRef
	SSHKeys []string // authorized public keys, served to the guest via MMDS
}

type ImageRef struct {
	Name string
}

type NetworkRef struct {
	Name string
}
