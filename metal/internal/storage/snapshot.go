package storage

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// Snapshot takes a named point-in-time snapshot of the VM's disk.
func (z *ZFS) Snapshot(ctx context.Context, vmID, name string) error {
	// zfs snapshot <vm>@<name>: create a read-only, point-in-time snapshot of
	// the zvol. Cheap: it just pins the current blocks.
	return hostcmd.Run(ctx, "zfs", "snapshot", z.snap(vmID, name))
}

// Snapshots lists the VM disk's snapshots (name after '@', sizes in MiB).
func (z *ZFS) Snapshots(ctx context.Context, vmID string) ([]SnapshotInfo, error) {
	// zfs list -Hp -t snapshot -o name,volsize,used,creation -r <vm>: -H drops the
	// header, -p prints exact numbers (bytes, and creation as a Unix epoch), -t
	// snapshot limits to snapshots, -o picks the columns, -r recurses into the
	// zvol (its snapshots).
	out, err := hostcmd.Output(ctx, "zfs", "list", "-Hp", "-t", "snapshot", "-o", "name,volsize,used,creation", "-r", z.vmDataset(vmID))
	if err != nil {
		return nil, notFoundAware(err)
	}
	var snaps []SnapshotInfo
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		f := strings.Fields(line)
		if len(f) != 4 {
			continue
		}
		_, name, ok := strings.Cut(f[0], "@")
		if !ok {
			continue
		}
		size, _ := strconv.ParseInt(f[1], 10, 64)
		used, _ := strconv.ParseInt(f[2], 10, 64)
		created, _ := strconv.ParseInt(f[3], 10, 64)
		snaps = append(snaps, SnapshotInfo{
			Name: name, SizeMiB: int(size >> 20), UsedMiB: int(used >> 20),
			CreatedAt: time.Unix(created, 0),
		})
	}
	return snaps, nil
}

// DeleteSnapshot removes one snapshot. ErrNotFound if it is unknown.
func (z *ZFS) DeleteSnapshot(ctx context.Context, vmID, name string) error {
	// zfs destroy <vm>@<name>: destroy one snapshot (no -r; a snapshot has no
	// children of its own).
	return notFoundAware(hostcmd.Run(ctx, "zfs", "destroy", z.snap(vmID, name)))
}

// Restore rolls the disk back to a snapshot, discarding newer snapshots. The
// caller must ensure the VM is stopped. ErrNotFound if the snapshot is unknown.
func (z *ZFS) Restore(ctx context.Context, vmID, name string) error {
	// zfs rollback -r <vm>@<name>: revert the zvol to the snapshot's contents;
	// -r destroys any snapshots newer than it (rollback requires that).
	return notFoundAware(hostcmd.Run(ctx, "zfs", "rollback", "-r", z.snap(vmID, name)))
}

// Usage reports the VM disk's provisioned/used size and snapshot count.
func (z *ZFS) Usage(ctx context.Context, vmID string) (Usage, error) {
	// zfs get -Hp -o property,value volsize,used <vm>: -H no header, -p exact
	// bytes, -o property,value emits a "<prop>\t<bytes>" row per requested prop
	// (volsize = provisioned, used = consumed).
	out, err := hostcmd.Output(ctx, "zfs", "get", "-Hp", "-o", "property,value", "volsize,used", z.vmDataset(vmID))
	if err != nil {
		return Usage{}, notFoundAware(err)
	}
	var u Usage
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		f := strings.Fields(line)
		if len(f) != 2 {
			continue
		}
		v, _ := strconv.ParseInt(f[1], 10, 64)
		switch f[0] {
		case "volsize":
			u.SizeMiB = int(v >> 20)
		case "used":
			u.UsedMiB = int(v >> 20)
		}
	}
	snaps, err := z.Snapshots(ctx, vmID)
	if err != nil {
		return u, err
	}
	u.Snapshots = len(snaps)
	return u, nil
}
