package firecracker

import (
	"context"
	"errors"
	"os"
	"syscall"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

// machine implements vm.VM as a client-side handle: a systemd unit (metal-vm@id)
// plus an API client over that unit's socket. It holds no child process.
type machine struct {
	cfg    vmConfig
	dir    string // per-VM state dir
	units  systemd.Manager
	images storage.Resolver
	net    network.Allocator
	api    *api.Client
}

func (d *Driver) newMachine(vc vmConfig) *machine {
	return &machine{
		cfg:    vc,
		dir:    d.cfg.vmDir(vc.ID),
		units:  d.units,
		images: d.images,
		net:    d.net,
		api:    api.New(vc.Sock),
	}
}

func (m *machine) ID() string { return m.cfg.ID }

// Start boots the guest.
func (m *machine) Start(ctx context.Context) error {
	return m.api.InstanceStart(ctx)
}

// Stop shuts the guest down: force sends SIGKILL via systemd, otherwise a
// graceful Ctrl+Alt+Del followed by waiting (bounded by ctx) for exit.
func (m *machine) Stop(ctx context.Context, force bool) error {
	if force {
		return m.units.Kill(ctx, m.cfg.ID, syscall.SIGKILL)
	}
	if err := m.api.SendCtrlAltDel(ctx); err != nil {
		return err
	}
	_, err := m.units.Wait(ctx, m.cfg.ID)
	return err
}

// Destroy stops the unit and frees the VM's network, disk and state. Best-effort
// so it is idempotent: a resource already gone is not an error.
func (m *machine) Destroy(ctx context.Context) error {
	_ = m.units.Stop(ctx, m.cfg.ID)
	_ = m.net.Release(ctx, m.cfg.ID)
	_ = m.images.Release(ctx, m.cfg.ID)
	return os.RemoveAll(m.dir)
}

func (m *machine) Wait(ctx context.Context) (vm.ExitStatus, error) {
	r, err := m.units.Wait(ctx, m.cfg.ID)
	if err != nil {
		return vm.ExitStatus{}, err
	}
	return vm.ExitStatus{Code: r.Code, Signal: r.Signal}, nil
}

func (m *machine) Info(ctx context.Context) (vm.Info, error) {
	st, err := m.units.Status(ctx, m.cfg.ID)
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
	if u, err := m.images.Usage(ctx, m.cfg.ID); err == nil {
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

// Snapshot is deferred: the current milestone excludes memory snapshotting.
func (m *machine) Snapshot(ctx context.Context, dir string, typ vm.SnapshotType) (vm.Snapshot, error) {
	return vm.Snapshot{}, errNotImplemented
}

// DiskSnapshot takes a named snapshot of the VM's rootfs disk.
func (m *machine) DiskSnapshot(ctx context.Context, name string) error {
	return storageErr(m.images.Snapshot(ctx, m.cfg.ID, name))
}

// DiskSnapshots lists the VM's disk snapshots.
func (m *machine) DiskSnapshots(ctx context.Context) ([]vm.DiskSnapshot, error) {
	snaps, err := m.images.Snapshots(ctx, m.cfg.ID)
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
	return storageErr(m.images.DeleteSnapshot(ctx, m.cfg.ID, name))
}

// RestoreDiskSnapshot rolls the disk back to a snapshot. The VM must be stopped
// (no live firecracker holding the disk), else ErrConflict.
func (m *machine) RestoreDiskSnapshot(ctx context.Context, name string) error {
	st, err := m.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	if s := m.state(ctx, st); s != vm.StateStopped && s != vm.StateFailed {
		return vm.ErrConflict
	}
	return storageErr(m.images.Restore(ctx, m.cfg.ID, name))
}

// storageErr maps storage's not-found sentinel to the vm-layer one so the API
// returns 404.
func storageErr(err error) error {
	if errors.Is(err, storage.ErrNotFound) {
		return vm.ErrNotFound
	}
	return err
}

var _ vm.VM = (*machine)(nil)
