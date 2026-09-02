package firecracker

import (
	"context"

	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

func (m *machine) Info(ctx context.Context) (vm.Info, error) {
	st, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return vm.Info{}, err
	}
	info := vm.Info{
		ID:      m.cfg.ID,
		State:   m.state(ctx, st),
		PID:     st.PID,
		IP:      m.cfg.IP,
		MAC:     m.cfg.MAC,
		Sock:    m.cfg.Sock,
		VCPUs:   m.cfg.Spec.VCPUs,
		MemMiB:  m.cfg.Spec.MemMiB,
		DiskMiB: m.cfg.Spec.DiskMiB,
		Image:   m.cfg.Spec.Image.Name,
		Network: m.cfg.Spec.Network.Name,
	}
	// Disk size/usage/snapshot count are best-effort: a missing zvol (e.g. a
	// half-built VM) must not fail Info.
	if u, err := m.d.images.Usage(ctx, m.cfg.ID); err == nil {
		if u.SizeMiB > 0 {
			info.DiskMiB = u.SizeMiB
		}
		info.DiskUsedMiB = u.UsedMiB
		info.Snapshots = u.Snapshots
	}
	return info, nil
}

// state derives the VM state from the unit plus firecracker: systemd knows if
// the process is up, firecracker knows if the guest has booted.
func (m *machine) state(ctx context.Context, st systemd.Status) vm.State {
	switch st.ActiveState {
	case "failed":
		return vm.StateFailed
	case "inactive", "deactivating":
		return vm.StateStopped
	}
	ii, err := m.api.InstanceInfo(ctx)
	if err != nil {
		return vm.StateRunning // process is up but state is unknowable
	}
	switch ii.State {
	case "Not started":
		return vm.StateCreated
	case "Paused":
		return vm.StatePaused
	default:
		return vm.StateRunning
	}
}
