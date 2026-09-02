// Package storage resolves logical image refs into drives prepared for a VM.
// The backend is ZFS: each VM's rootfs is a clone of a base zvol's @ready
// snapshot, optionally grown to a requested size, exposed as a block device
// inside the chroot and owned by the VM's uid; the kernel is a hard-linked file.
package storage

import (
	"context"
	"errors"
	"time"
)

// ErrNotFound is returned when a VM disk, snapshot, or image does not exist.
var ErrNotFound = errors.New("storage: not found")

// ErrInUse is returned when an image cannot be removed because VMs cloned from it
// still exist.
var ErrInUse = errors.New("storage: in use")

type Resolver interface {
	// Prepare provisions the VM's kernel + rootfs and returns what firecracker
	// needs to boot.
	Prepare(ctx context.Context, req Request) (BootConfig, error)
	// PrepareRootfs provisions only the VM's rootfs block device, for restoring or
	// warm-starting from a snapshot. The guest kernel is inside the memory snapshot.
	PrepareRootfs(ctx context.Context, req Request) error
	// Release frees whatever Prepare allocated for vmID.
	Release(ctx context.Context, vmID string) error
	// Resize grows the VM disk to diskMiB. Grow-only; never shrinks.
	Resize(ctx context.Context, vmID string, diskMiB int) error
	// Snapshot takes a named point-in-time snapshot of the VM's disk.
	Snapshot(ctx context.Context, vmID, name string) error
	// Snapshots lists the VM's disk snapshots.
	Snapshots(ctx context.Context, vmID string) ([]SnapshotInfo, error)
	// DeleteSnapshot removes one snapshot. ErrNotFound if it is unknown.
	DeleteSnapshot(ctx context.Context, vmID, name string) error
	// Restore rolls the disk back to a snapshot, discarding newer snapshots.
	// The VM must be stopped; ErrNotFound if the snapshot is unknown.
	Restore(ctx context.Context, vmID, name string) error
	// Usage reports the VM disk's provisioned/used size and snapshot count.
	Usage(ctx context.Context, vmID string) (Usage, error)
	// Promote builds a standalone warm image from a VM's snapshot.
	Promote(ctx context.Context, req PromoteRequest) error
	// ImageMemory returns an image's state and memory file paths and whether the
	// image is warm (it carries a memory capture).
	ImageMemory(ref string) (state, mem string, warm bool)
	// Images lists the available images.
	Images(ctx context.Context) ([]ImageInfo, error)
	// DeleteImage removes an image. ErrInUse if VMs cloned from it still exist,
	// ErrNotFound if it is unknown.
	DeleteImage(ctx context.Context, ref string) error
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

// SnapshotInfo describes one disk snapshot.
type SnapshotInfo struct {
	Name      string
	SizeMiB   int
	UsedMiB   int
	CreatedAt time.Time
}

// Usage is a VM disk's provisioned size, consumed space, and snapshot count.
type Usage struct {
	SizeMiB   int
	UsedMiB   int
	Snapshots int
}

// ImageInfo describes one image. A warm image also carries a memory capture, so
// VMs from it start from restored RAM instead of a cold boot.
type ImageInfo struct {
	Ref       string
	Warm      bool
	SizeMiB   int
	CreatedAt time.Time
}

// PromoteRequest asks to build image Ref from srcVMID's SnapName snapshot. The
// state and memory files are the source VM's snapshot files, copied into the image.
type PromoteRequest struct {
	SrcVMID   string
	SnapName  string
	SrcRef    string // source image ref, so the kernel can be copied to the new image
	Ref       string // new image ref
	StateFile string // source device/vCPU state file (host path)
	MemFile   string // source guest memory file (host path)
}
