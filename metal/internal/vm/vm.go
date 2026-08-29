package vm

import "context"

type VM interface {
	ID() string
	Start(ctx context.Context) error
	// force=false: graceful Ctrl+Alt+Del bounded by ctx; force=true: SIGKILL.
	Stop(ctx context.Context, force bool) error
	Destroy(ctx context.Context) error
	Wait(ctx context.Context) (ExitStatus, error)
	Snapshot(ctx context.Context, dir string, typ SnapshotType) (Snapshot, error)
	Info(ctx context.Context) (Info, error)
}

type Info struct {
	ID      string
	State   State
	PID     int
	IP      string
	MAC     string
	Sock    string
	VCPUs   int
	MemMiB  int
	DiskMiB int
	Image   string
	Network string
}

type ExitStatus struct {
	Code   int
	Signal string
}
