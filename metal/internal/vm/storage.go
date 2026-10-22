package vm

import "context"

// Storage manages virtual machine disks.
type Storage interface {
	HasDisk(context.Context, string) (bool, error)
	DiskUsage(context.Context, string) (DiskUsage, error)
	ResizeDisk(context.Context, string, int) error
	Release(context.Context, string) error
}

// DiskUsage contains disk size and allocation values.
type DiskUsage struct {
	SizeMiB int `json:"size_mib"`
	UsedMiB int `json:"used_mib"`
}

// Snapshots stages and transfers virtual machine snapshots.
type Snapshots interface {
	Stage(context.Context, SnapshotRequest) (StagedSnapshot, error)
	StageSleepMemory(context.Context, string, string) error
	StageSleepDisk(context.Context, SnapshotRequest, string) error
	DeleteSnapshot(context.Context, string) error
	UploadSleepMemory(context.Context, string, string) (SleepArtifact, error)
	UploadSleepDisk(context.Context, string, string) (SleepArtifact, error)
	DownloadSleepMemory(ctx context.Context, directory, source string, artifact SleepArtifact) error
	RestoreSleepDisk(ctx context.Context, identifier, source string, artifact SleepArtifact) error
}

// SleepArtifact binds a remote object to its uncompressed content.
type SleepArtifact struct {
	SizeBytes       int64  `json:"size_bytes"`
	StoredSizeBytes int64  `json:"stored_size_bytes"`
	SHA256          string `json:"sha256"`
}

// SnapshotRequest identifies the virtual machine and kernel to stage.
type SnapshotRequest struct {
	VirtualMachineID string
	KernelSHA256     string
}

// StagedSnapshot describes a staged machine image.
type StagedSnapshot struct {
	ID                     string
	SourceVirtualMachineID string
	RootfsSizeBytes        int64
	KernelSizeBytes        int64
}
