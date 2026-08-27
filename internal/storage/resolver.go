// Package storage resolves logical image refs into drives prepared for a VM.
// Provisional: the current backend hard-links files into the jailer chroot;
// LVM/ZFS backends (TBD) will activate or clone volumes, so nothing here may
// assume plain files.
package storage

import "context"

type Resolver interface {
	// Prepare makes vmID's kernel + rootfs available under chrootRoot and
	// returns what the driver needs to configure firecracker.
	Prepare(ctx context.Context, vmID, ref, chrootRoot string) (Prepared, error)
	// Release frees whatever Prepare allocated for vmID.
	Release(ctx context.Context, vmID string) error
}

type Prepared struct {
	KernelPath string // path inside the chroot
	BootArgs   string
	Drives     []PreparedDrive
}

type PreparedDrive struct {
	PathInChroot string
	ReadOnly     bool
	IsRoot       bool
}
