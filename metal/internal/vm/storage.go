package vm

import "context"

// Storage manages virtual machine disks.
type Storage interface {
	DiskUsage(context.Context, string) (DiskUsage, error)
	ResizeDisk(context.Context, string, int) error
	Release(context.Context, string) error
}

// DiskUsage contains disk size and allocation values.
type DiskUsage struct {
	SizeMiB int
	UsedMiB int
}

// Snapshots stages machine image snapshots.
type Snapshots interface {
	Stage(context.Context, SnapshotRequest) (StagedSnapshot, error)
}

// SnapshotRequest identifies the virtual machine and image to stage.
type SnapshotRequest struct {
	VirtualMachineID string
	ImageReference   string
	IsDiskEncrypted  bool
}

// StagedSnapshot describes a staged machine image.
type StagedSnapshot struct {
	ID                     string
	SourceVirtualMachineID string
	RootfsSizeBytes        int64
	KernelSizeBytes        int64
	InitrdSizeBytes        int64
}
