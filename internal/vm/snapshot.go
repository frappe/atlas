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
