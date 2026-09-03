package firecracker

import (
	"context"
	"errors"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// ResizeDisk grows a disk and updates a running guest.
func (m *machine) ResizeDisk(ctx context.Context, diskMiB int) error {
	unlock, err := m.d.operationLocks.lock(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	defer unlock()

	usage, err := m.d.virtualMachineStorage.DiskUsage(ctx, m.cfg.ID)
	if err != nil {
		return storageErr(err)
	}
	switch {
	case diskMiB < usage.SizeMiB:
		return vm.ErrConflict
	case diskMiB == usage.SizeMiB:
		return nil
	}
	if err := m.d.virtualMachineStorage.ResizeDisk(ctx, m.cfg.ID, diskMiB); err != nil {
		return storageErr(err)
	}
	unitStatus, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	if m.state(ctx, unitStatus) == vm.StateRunning {
		if err := m.api.PatchDrive(ctx, api.PartialDrive{DriveID: rootDriveIdentifier, PathOnHost: rootDrivePath}); err != nil {
			return err
		}
	}
	// Preserve changes made after this handle was loaded.
	configuration, err := m.d.cfg.readVMConfig(m.cfg.ID)
	if err != nil {
		return err
	}
	configuration.Spec.DiskMiB = diskMiB
	m.cfg = configuration
	return m.d.cfg.writeVMConfig(configuration)
}

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
