package vm

type Spec struct {
	VCPUs   int
	MemMiB  int
	DiskMiB int
	Image   ImageRef
	Network NetworkRef
}

type ImageRef struct {
	Name string
}

type NetworkRef struct {
	Name string
}
