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
	// Pause halts the guest's vCPUs. Returns ErrConflict unless the VM is running.
	Pause(ctx context.Context) error
	// Resume returns a paused guest to running. Returns ErrConflict otherwise.
	Resume(ctx context.Context) error
	// Snapshot takes a named snapshot of the VM's disk. When memory is true it also
	// captures RAM and device state, paired with the disk snapshot.
	Snapshot(ctx context.Context, name string, memory bool) error
	// Snapshots lists the VM's snapshots.
	Snapshots(ctx context.Context) ([]Snapshot, error)
	// DeleteSnapshot removes one snapshot and its memory files, if any.
	DeleteSnapshot(ctx context.Context, name string) error
	// RestoreSnapshot rolls the VM back to a snapshot. A memory snapshot also
	// reloads RAM so the VM resumes at the captured instant; a disk-only snapshot
	// leaves the VM stopped to cold-boot from the rolled-back disk.
	RestoreSnapshot(ctx context.Context, name string) error
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
