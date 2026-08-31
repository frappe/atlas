package vm

import "context"

type VM interface {
	ID() string
	Start(ctx context.Context) error
	// force=false: graceful Ctrl+Alt+Del bounded by ctx; force=true: SIGKILL.
	Stop(ctx context.Context, force bool) error
	Destroy(ctx context.Context) error
	Wait(ctx context.Context) (ExitStatus, error)
	// Resize grows the VM's disk to diskMiB. Grow-only; returns ErrConflict on
	// a smaller request.
	Resize(ctx context.Context, diskMiB int) error
	Snapshot(ctx context.Context, dir string, typ SnapshotType) (Snapshot, error)
	// DiskSnapshot takes a named snapshot of the VM's rootfs disk.
	DiskSnapshot(ctx context.Context, name string) error
	// DiskSnapshots lists the VM's disk snapshots.
	DiskSnapshots(ctx context.Context) ([]DiskSnapshot, error)
	// DeleteDiskSnapshot removes one disk snapshot.
	DeleteDiskSnapshot(ctx context.Context, name string) error
	// RestoreDiskSnapshot rolls the disk back to a snapshot. The VM must be
	// stopped; returns ErrConflict otherwise.
	RestoreDiskSnapshot(ctx context.Context, name string) error
	Info(ctx context.Context) (Info, error)
}

type Info struct {
	ID          string
	State       State
	PID         int
	IP          string
	MAC         string
	Sock        string
	VCPUs       int
	MemMiB      int
	DiskMiB     int
	DiskUsedMiB int
	Snapshots   int
	Image       string
	Network     string
}

type ExitStatus struct {
	Code   int
	Signal string
}
