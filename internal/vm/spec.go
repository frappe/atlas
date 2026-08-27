package vm

type Spec struct {
	VCPUs   int
	MemMiB  int
	Image   string // logical ref, resolved by the driver's storage dep
	Network string // logical ref, resolved by the driver's network dep
}
