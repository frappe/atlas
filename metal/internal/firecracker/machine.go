package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"syscall"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
)

const defaultStopTimeout = 30 * time.Second

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

// Start boots a virtual machine.
func (m *machine) Start(ctx context.Context) error {
	unlock, err := m.d.operationLocks.lock(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	defer unlock()

	return m.startUnlocked(ctx)
}

func (m *machine) startUnlocked(ctx context.Context) error {
	if m.d.hasMatchingMemorySnapshot(m.cfg.Spec) {
		err := m.d.launchWarmImage(ctx, m.cfg, m.cfg.Spec.Image.Name)
		if err == nil {
			m.recordImageUse()
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		log.Printf("firecracker: warm boot for vm %s failed, use cold boot: %v", m.cfg.ID, err)
		if err := m.d.virtualMachineStorage.Release(ctx, m.cfg.ID); err != nil {
			return err
		}
	}

	if err := m.d.relaunch(ctx, m.cfg); err != nil {
		return err
	}
	if err := m.api.InstanceStart(ctx); err != nil {
		return err
	}
	m.recordImageUse()
	return nil
}

func (m *machine) recordImageUse() {
	if err := m.d.imageStore.RecordImageUse(m.cfg.Spec.Image.Name, time.Now()); err != nil {
		log.Printf("firecracker: record image use for vm %s: %v", m.cfg.ID, err)
	}
}

// Stop shuts down the guest or kills it after the timeout.
func (m *machine) Stop(ctx context.Context) error {
	unlock, err := m.d.operationLocks.lock(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	defer unlock()

	return m.stopUnlocked(ctx)
}

func (m *machine) stopUnlocked(ctx context.Context) error {
	if err := m.shutdownGuest(ctx); err != nil {
		return err
	}
	if _, err := m.d.units.Wait(ctx, m.cfg.ID); err != nil {
		return err
	}
	_ = m.d.consoleBroker.Close(m.cfg.ID)

	// Clear the failed state after an intentional process stop.
	return m.d.units.ResetFailed(ctx, m.cfg.ID)
}

func (m *machine) killUnlocked(ctx context.Context) error {
	if err := m.d.units.Kill(ctx, m.cfg.ID, syscall.SIGKILL); err != nil {
		return err
	}
	if _, err := m.d.units.Wait(ctx, m.cfg.ID); err != nil {
		return err
	}
	_ = m.d.consoleBroker.Close(m.cfg.ID)
	return m.d.units.ResetFailed(ctx, m.cfg.ID)
}

func (m *machine) shutdownGuest(ctx context.Context) error {
	if m.api.SendCtrlAltDel(ctx) == nil {
		wait, cancel := context.WithTimeout(ctx, m.stopTimeout)
		defer cancel()
		if _, err := m.d.units.Wait(wait, m.cfg.ID); err == nil {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	return m.d.units.Kill(ctx, m.cfg.ID, syscall.SIGKILL)
}

// Destroy removes all virtual machine resources.
func (m *machine) Destroy(ctx context.Context) error {
	unlock, err := m.d.operationLocks.lock(ctx, m.cfg.ID)
	if err != nil {
		return err
	}
	defer unlock()

	return m.destroyUnlocked(ctx)
}

func (m *machine) destroyUnlocked(ctx context.Context) error {
	configuration, err := m.d.cfg.readVMConfig(m.cfg.ID)
	if errors.Is(err, vm.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if configuration.DesiredState != vm.StateDestroyed {
		configuration.DesiredState = vm.StateDestroyed
		if err := m.d.cfg.writeVMConfig(configuration); err != nil {
			return err
		}
	}

	if !configuration.Cleanup.Systemd {
		if err := m.cleanupSystemd(ctx); err != nil {
			return err
		}
		configuration.Cleanup.Systemd = true
		if err := m.d.cfg.writeVMConfig(configuration); err != nil {
			return err
		}
	}
	if !configuration.Cleanup.Network {
		if err := m.d.networkAllocator.Release(ctx, configuration.ID); err != nil {
			return fmt.Errorf("release VM network: %w", err)
		}
		configuration.Cleanup.Network = true
		if err := m.d.cfg.writeVMConfig(configuration); err != nil {
			return err
		}
	}
	if !configuration.Cleanup.Storage {
		if err := m.d.virtualMachineStorage.Release(ctx, configuration.ID); err != nil {
			return fmt.Errorf("release VM storage: %w", err)
		}
		configuration.Cleanup.Storage = true
		if err := m.d.cfg.writeVMConfig(configuration); err != nil {
			return err
		}
	}

	if err := os.Remove(m.d.cfg.sockPath(configuration.ID)); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove VM socket: %w", err)
	}
	return os.RemoveAll(m.d.cfg.vmDir(configuration.ID))
}

func (m *machine) cleanupSystemd(ctx context.Context) error {
	if err := m.d.units.Stop(ctx, m.cfg.ID); err != nil {
		return fmt.Errorf("stop VM unit: %w", err)
	}
	_ = m.d.consoleBroker.Close(m.cfg.ID)
	if err := m.d.units.ResetFailed(ctx, m.cfg.ID); err != nil {
		return fmt.Errorf("reset VM unit: %w", err)
	}
	return nil
}

func (m *machine) Wait(ctx context.Context) (vm.ExitStatus, error) {
	r, err := m.d.units.Wait(ctx, m.cfg.ID)
	if err != nil {
		return vm.ExitStatus{}, err
	}
	return vm.ExitStatus{Code: r.Code, Signal: r.Signal}, nil
}

var _ vm.VM = (*machine)(nil)
