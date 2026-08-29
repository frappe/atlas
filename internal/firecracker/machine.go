package firecracker

import (
	"context"
	"os"
	"syscall"

	"github.com/frappe/metal/internal/firecracker/api"
	"github.com/frappe/metal/internal/network"
	"github.com/frappe/metal/internal/storage"
	"github.com/frappe/metal/internal/systemd"
	"github.com/frappe/metal/internal/vm"
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
	return vm.Info{
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
	}, nil
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

// Snapshot is deferred: the current milestone excludes snapshotting.
func (m *machine) Snapshot(ctx context.Context, dir string, typ vm.SnapshotType) (vm.Snapshot, error) {
	return vm.Snapshot{}, errNotImplemented
}

var _ vm.VM = (*machine)(nil)
