package vm

type SnapshotType string

const (
	SnapshotFull SnapshotType = "full"
	SnapshotDiff SnapshotType = "diff"
)

type Snapshot struct {
	MemFilePath   string
	StateFilePath string
	Type          SnapshotType
}

// DiskSnapshot is a point-in-time snapshot of a VM's rootfs disk (distinct from
// the memory Snapshot above). Sizes are in MiB.
type DiskSnapshot struct {
	Name    string
	SizeMiB int
	UsedMiB int
}
