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

// storageErr maps storage's sentinels to the vm-layer ones so the API returns the
// right status: 404 for not-found, 409 for in-use.
func storageErr(err error) error {
	switch {
	case errors.Is(err, storage.ErrNotFound):
		return vm.ErrNotFound
	case errors.Is(err, storage.ErrInUse):
		return vm.ErrConflict
	default:
		return err
	}
}
