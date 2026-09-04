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
		ID:                m.cfg.ID,
		State:             m.state(ctx, st),
		DesiredState:      m.cfg.DesiredState,
		VCPUs:             m.cfg.Spec.VCPUs,
		MemoryMiB:         m.cfg.Spec.MemoryMiB,
		DiskMiB:           m.cfg.Spec.DiskMiB,
		Image:             m.cfg.Spec.Image,
		SSHKeys:           append([]string(nil), m.cfg.Spec.SSHKeys...),
		Hostname:          m.cfg.Spec.Hostname,
		MAC:               m.cfg.MAC,
		PublicIPv4:        m.cfg.Spec.Network.PublicIPv4,
		WireGuardMeshIPv6: m.cfg.Spec.Network.WireGuardMeshIPv6,
		Egress:            m.cfg.Spec.Network.Egress,
	}

	if usage, err := m.d.virtualMachineStorage.DiskUsage(ctx, m.cfg.ID); err == nil {
		if usage.SizeMiB > 0 {
			info.DiskMiB = usage.SizeMiB
		}
		info.DiskUsedMiB = usage.UsedMiB
	}

	if status, ok := m.d.cfg.readStatus(m.cfg.ID); ok && status.Error != "" {
		info.Error = status.Error
		if info.State != vm.StateRunning {
			info.State = vm.StateFailed
		}
	}
	return info, nil
}

func (m *machine) state(ctx context.Context, st systemd.Status) vm.State {
	switch st.ActiveState {
	case "failed":
		return vm.StateFailed
	case "inactive", "deactivating":
		return vm.StateStopped
	case "active":
	default:
		return vm.StateUnknown
	}

	instance, err := m.api.InstanceInfo(ctx)
	if err != nil {
		return vm.StateUnknown
	}
	switch instance.State {
	case "Not started":
		return vm.StateCreated
	case "Running":
		return vm.StateRunning
	case "Paused":
		return vm.StatePaused
	default:
		return vm.StateUnknown
	}
}
