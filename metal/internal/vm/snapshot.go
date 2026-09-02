package vm

import "time"

// Snapshot is a point-in-time snapshot of a VM. A memory snapshot also captures
// guest RAM and device state, paired with the disk snapshot, so a restore resumes
// at the captured instant.
type Snapshot struct {
	Name      string
	Memory    bool
	SizeMiB   int
	UsedMiB   int
	CreatedAt time.Time
}
