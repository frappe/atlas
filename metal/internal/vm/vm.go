package vm

import "context"

// VM controls one virtual machine.
type VM interface {
	ID() string
	Start(ctx context.Context) error
	Stop(ctx context.Context) error
	Pause(ctx context.Context) error
	Resume(ctx context.Context) error
	Destroy(ctx context.Context) error
	Wait(ctx context.Context) (ExitStatus, error)
	ResizeDisk(ctx context.Context, diskMiB int) error
	Info(ctx context.Context) (Info, error)
}

// Info describes a virtual machine.
type Info struct {
	ID                string
	State             State
	DesiredState      State
	Error             string
	VCPUs             int
	MemoryMiB         int
	DiskMiB           int
	DiskUsedMiB       int
	Image             ImageRef
	SSHKeys           []string
	Hostname          string
	Metadata          map[string]string
	MAC               string
	PublicIPv4        string
	WireGuardMeshIPv6 string
	Egress            Egress
}

// ExitStatus describes a stopped virtual machine process.
type ExitStatus struct {
	Code   int
	Signal string
}
