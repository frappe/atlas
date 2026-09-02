package firecracker

import (
	"context"
	"os"
	"syscall"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
)

// defaultStopTimeout is how long a guest gets to shut itself down after
// Ctrl+Alt+Del before metald escalates to a systemd stop job.
const defaultStopTimeout = 30 * time.Second

// machine implements vm.VM as a client-side handle: a systemd unit (metal-vm@id)
// plus an API client over that unit's socket. It holds no child process. It
// reaches its dependencies through the owning Driver.
type machine struct {
	d           *Driver
	cfg         vmConfig
	api         *api.Client
	stopTimeout time.Duration
}

func (d *Driver) newMachine(vc vmConfig) *machine {
	return &machine{
		d:           d,
		cfg:         vc,
		api:         api.New(vc.Sock),
		stopTimeout: defaultStopTimeout,
	}
}

func (m *machine) ID() string { return m.cfg.ID }

// Start boots the guest. A never-run warm VM loads its image's memory instead of
// cold-booting. A fully stopped VM (its jailer process is gone) is relaunched
// first; a running VM is a conflict.
func (m *machine) Start(ctx context.Context) error {
	if ref, ok := m.d.cfg.readWarmMark(m.cfg.ID); ok {
		if err := m.d.warmLaunch(ctx, m.cfg, ref); err != nil {
			return err
		}
		m.d.cfg.clearWarmMark(m.cfg.ID)
		return nil
	}
	st, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	switch m.state(ctx, st) {
	case vm.StateRunning:
		return vm.ErrConflict
	case vm.StateStopped, vm.StateFailed:
		if err := m.d.relaunch(ctx, m.cfg); err != nil {
			return err
		}
	}
	return m.api.InstanceStart(ctx)
}

// Stop shuts the guest down: force sends SIGKILL via systemd, otherwise the
// guest is asked to shut itself down first. Stop returns only once the process
// has exited, so the VM state it leaves behind is truthful.
func (m *machine) Stop(ctx context.Context, force bool) error {
	if force {
		if err := m.d.units.Kill(ctx, m.cfg.ID, syscall.SIGKILL); err != nil {
			return err
		}
	} else if err := m.shutdownGuest(ctx); err != nil {
		return err
	}
	if _, err := m.d.units.Wait(ctx, m.cfg.ID); err != nil {
		return err
	}
	// systemd marks a unit failed when its main process is killed or exits
	// non-zero, which a deliberate stop always is. Clear that, so a stopped VM
	// reports StateStopped and only a real crash reports StateFailed.
	return m.d.units.ResetFailed(ctx, m.cfg.ID)
}

// shutdownGuest sends Ctrl+Alt+Del and gives the guest stopTimeout to shut
// itself down. Firecracker delivers the keys through its emulated i8042
// controller, so a guest kernel built without an i8042 keyboard driver never
// sees them. The wait is therefore bounded, and metald escalates to a systemd
// stop job when the guest does not exit.
func (m *machine) shutdownGuest(ctx context.Context) error {
	if err := m.api.SendCtrlAltDel(ctx); err != nil {
		return err
	}
	wait, cancel := context.WithTimeout(ctx, m.stopTimeout)
	defer cancel()
	if _, err := m.d.units.Wait(wait, m.cfg.ID); err == nil {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return m.d.units.Stop(ctx, m.cfg.ID)
}

// Destroy stops the unit and frees the VM's network, disk and state. Best-effort
// so it is idempotent: a resource already gone is not an error.
func (m *machine) Destroy(ctx context.Context) error {
	_ = m.d.units.Stop(ctx, m.cfg.ID)
	_ = m.d.units.ResetFailed(ctx, m.cfg.ID) // do not leave a failed unit behind
	_ = m.d.net.Release(ctx, m.cfg.ID)
	_ = m.d.images.Release(ctx, m.cfg.ID)
	_ = os.Remove(m.d.cfg.sockPath(m.cfg.ID))
	return os.RemoveAll(m.d.cfg.vmDir(m.cfg.ID))
}

func (m *machine) Wait(ctx context.Context) (vm.ExitStatus, error) {
	r, err := m.d.units.Wait(ctx, m.cfg.ID)
	if err != nil {
		return vm.ExitStatus{}, err
	}
	return vm.ExitStatus{Code: r.Code, Signal: r.Signal}, nil
}

var _ vm.VM = (*machine)(nil)
