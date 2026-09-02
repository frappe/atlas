package firecracker

import (
	"context"
	"errors"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// Resize grows the VM's disk to diskMiB and makes a running guest see it.
// Grow-only: a smaller request is ErrConflict; an equal one is a no-op.
func (m *machine) Resize(ctx context.Context, diskMiB int) error {
	u, err := m.d.images.Usage(ctx, m.cfg.ID)
	if err != nil {
		return storageErr(err)
	}
	switch {
	case diskMiB < u.SizeMiB:
		return vm.ErrConflict
	case diskMiB == u.SizeMiB:
		return nil
	}
	if err := m.d.images.Resize(ctx, m.cfg.ID, diskMiB); err != nil {
		return storageErr(err)
	}
	// If the guest is running, have firecracker rescan the grown block device.
	st, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	if m.state(ctx, st) == vm.StateRunning {
		if err := m.api.PatchDrive(ctx, api.PartialDrive{DriveID: rootDriveID, PathOnHost: rootDrivePath}); err != nil {
			return err
		}
	}
	// Persist the new size so Load/List/Info stay truthful.
	m.cfg.Spec.DiskMiB = diskMiB
	return m.d.cfg.writeVMConfig(m.cfg)
}

// DiskSnapshot takes a named snapshot of the VM's rootfs disk.
func (m *machine) DiskSnapshot(ctx context.Context, name string) error {
	return storageErr(m.d.images.Snapshot(ctx, m.cfg.ID, name))
}

// DiskSnapshots lists the VM's disk snapshots.
func (m *machine) DiskSnapshots(ctx context.Context) ([]vm.DiskSnapshot, error) {
	snaps, err := m.d.images.Snapshots(ctx, m.cfg.ID)
	if err != nil {
		return nil, storageErr(err)
	}
	out := make([]vm.DiskSnapshot, len(snaps))
	for i, s := range snaps {
		out[i] = vm.DiskSnapshot{Name: s.Name, SizeMiB: s.SizeMiB, UsedMiB: s.UsedMiB}
	}
	return out, nil
}

// DeleteDiskSnapshot removes one disk snapshot.
func (m *machine) DeleteDiskSnapshot(ctx context.Context, name string) error {
	return storageErr(m.d.images.DeleteSnapshot(ctx, m.cfg.ID, name))
}

// RestoreDiskSnapshot rolls the disk back to a snapshot. The VM must be stopped
// (no live firecracker holding the disk), else ErrConflict.
func (m *machine) RestoreDiskSnapshot(ctx context.Context, name string) error {
	st, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	if s := m.state(ctx, st); s != vm.StateStopped && s != vm.StateFailed {
		return vm.ErrConflict
	}
	return storageErr(m.d.images.Restore(ctx, m.cfg.ID, name))
}

// storageErr maps storage's not-found sentinel to the vm-layer one so the API
// returns 404.
func storageErr(err error) error {
	if errors.Is(err, storage.ErrNotFound) {
		return vm.ErrNotFound
	}
	return err
}
