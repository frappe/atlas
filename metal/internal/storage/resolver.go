// Package storage resolves logical image refs into drives prepared for a VM.
// The backend is ZFS: each VM's rootfs is a clone of a base zvol's @ready
// snapshot, optionally grown to a requested size, exposed as a block device
// inside the chroot and owned by the VM's uid; the kernel is a hard-linked file.
package storage

import "context"

type Resolver interface {
	// Prepare provisions the VM's kernel + rootfs and returns what firecracker
	// needs to boot.
	Prepare(ctx context.Context, req Request) (BootConfig, error)
	// Release frees whatever Prepare allocated for vmID.
	Release(ctx context.Context, vmID string) error
}

type Request struct {
	VMID       string
	Ref        string
	ChrootRoot string
	UID, GID   uint32
	DiskMiB    int // grow the rootfs to this size; 0 keeps the base size
}

// BootConfig is the resolved image: what firecracker needs to boot the VM.
type BootConfig struct {
	Kernel     string // kernel path inside the chroot
	KernelArgs string
	Drives     []Drive
}

type Drive struct {
	Path     string // block device or file path inside the chroot
	ReadOnly bool
	Root     bool
}
