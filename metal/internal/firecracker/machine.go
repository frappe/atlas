package firecracker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"syscall"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// defaultStopTimeout bounds graceful guest shutdown after Ctrl+Alt+Del.
const defaultStopTimeout = 30 * time.Second

// machine binds a runtime to one VM for one operation. systemd owns process
// lifetime, while Firecracker owns guest state.
type machine struct {
	runtime     *Runtime
	input       vm.RuntimeMachine
	api         *api.Client
	stopTimeout time.Duration
}

// newMachine returns a handle for one VM.
func (runtime *Runtime) newMachine(input vm.RuntimeMachine) *machine {
	return &machine{
		runtime:     runtime,
		input:       input,
		api:         api.New(runtime.configuration.socketPath(input.ID)),
		stopTimeout: defaultStopTimeout,
	}
}

// status reports the VM state from the unit and the guest.
func (m *machine) status(ctx context.Context) (vm.State, error) {
	unitStatus, err := m.runtime.units.Status(ctx, m.input.ID)
	if err != nil {
		return "", err
	}

	return m.state(ctx, unitStatus)
}

// state maps a systemd unit state to a VM state. Only an active unit is asked
// for Firecracker state.
func (m *machine) state(ctx context.Context, status platform.Status) (vm.State, error) {
	switch status.ActiveState {
	case "failed":
		return vm.StateFailed, nil
	case "inactive", "deactivating":
		return vm.StateStopped, nil
	case "active":
	default:
		return vm.StateUnknown, nil
	}

	instance, err := m.api.InstanceInfo(ctx)
	if err != nil {
		return vm.StateUnknown, fmt.Errorf("inspect Firecracker instance: %w", err)
	}

	switch instance.State {
	case "Not started":
		return vm.StateCreated, nil
	case "Running":
		return vm.StateRunning, nil
	case "Paused":
		return vm.StatePaused, nil
	default:
		return vm.StateUnknown, nil
	}
}

// Start resumes the shared warm image only on the first boot, before the VM has
// its own disk. A later start cold boots the existing disk. It never restores VM
// saved state.
func (m *machine) Start(ctx context.Context) error {
	if m.input.Specification.Rescue.Enabled {
		return m.coldBoot(ctx)
	}

	hasDisk, err := m.runtime.virtualMachineStorage.HasDisk(ctx, m.input.ID)
	if err != nil {
		return err
	}

	// Warm memory matches only a fresh clone of the image disk. Resuming it over a used disk corrupts the guest file system.
	if !hasDisk && m.runtime.hasMatchingMemorySnapshot(m.input.Specification) {
		err := m.runtime.launchWarmImage(ctx, m.input, m.input.Specification.Image.Name)
		if err == nil {
			m.recordImageUse()
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if errors.Is(err, storage.ErrDiskNotFresh) {
			m.runtime.logger.Warn("VM disk changed, using cold boot", "virtual_machine_id", m.input.ID, "error", err)
			return m.coldBoot(ctx)
		}

		m.runtime.logger.Warn("warm boot failed, using cold boot", "virtual_machine_id", m.input.ID, "error", err)
		if err := m.runtime.virtualMachineStorage.Release(ctx, m.input.ID); err != nil {
			return err
		}
	}

	return m.coldBoot(ctx)
}

// coldBoot discards the previous jail and boots the guest fresh.
func (m *machine) coldBoot(ctx context.Context) error {
	if err := m.runtime.relaunch(ctx, m.input); err != nil {
		return err
	}
	if err := m.api.InstanceStart(ctx); err != nil {
		return err
	}
	m.recordImageUse()

	return nil
}

func (m *machine) savedStateRequirement() savedStateRequirement {
	return savedStateRequirement{
		VirtualMachineID:         m.input.ID,
		UserID:                   m.input.UserID,
		SpecificationGeneration:  m.input.SpecificationGeneration,
		RestartGeneration:        m.input.RestartGeneration,
		RescueGeneration:         m.input.RescueGeneration,
		FirecrackerCompatibility: m.runtime.firecrackerCompatibility(),
	}
}

// restoreSavedState loads valid VM-local state into a new jail.
func (m *machine) restoreSavedState(ctx context.Context, resume bool) (bool, error) {
	snapshot, err := m.runtime.configuration.loadSavedState(m.savedStateRequirement())
	if errors.Is(err, errSavedStateNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}

	metadata := m.input.Specification.MetadataServiceData(
		m.input.ID, m.input.NetworkInterface.GuestIPAddress, m.input.NetworkInterface.MACAddress,
	)
	if err := m.runtime.launchMemorySnapshot(ctx, m.input, "", snapshot.StatePath, snapshot.MemoryPath, metadata, resume); err != nil {
		return false, err
	}
	if err := m.runtime.removeSavedState(m.input.ID); err != nil {
		return false, fmt.Errorf("remove restored saved state: %w", err)
	}

	return true, nil
}

func (m *machine) savedState() (savedState, bool) {
	snapshot, err := m.runtime.configuration.loadSavedState(m.savedStateRequirement())
	if err != nil {
		return savedState{}, false
	}
	return snapshot, true
}

// startFromSavedState restores required VM-local state without a cold boot.
func (m *machine) startFromSavedState(ctx context.Context, resume bool) error {
	if m.input.Specification.Rescue.Enabled {
		return vm.ErrConflict
	}

	restored, err := m.restoreSavedState(ctx, resume)
	if err != nil {
		return err
	}
	if !restored {
		return fmt.Errorf("restore saved VM state: %w", errSavedStateNotFound)
	}
	m.recordImageUse()

	return nil
}

// Stop shuts down the guest and deletes saved state.
func (m *machine) Stop(ctx context.Context) error {
	if err := m.runtime.clearRescueBoot(m.input.ID); err != nil {
		return err
	}

	unitStatus, err := m.runtime.units.Status(ctx, m.input.ID)
	if err != nil {
		return err
	}
	if unitStatus.ActiveState != "active" {
		if err := m.runtime.removeSavedState(m.input.ID); err != nil {
			return err
		}
		return m.runtime.units.ResetFailed(ctx, m.input.ID)
	}
	if err := m.shutdownGuest(ctx); err != nil {
		return err
	}
	if _, err := m.runtime.units.Wait(ctx, m.input.ID); err != nil {
		return err
	}
	_ = m.runtime.serialBroker.Close(m.input.ID)

	if err := m.runtime.removeSavedState(m.input.ID); err != nil {
		return err
	}

	// An intentional stop leaves the unit failed, because the process was killed.
	return m.runtime.units.ResetFailed(ctx, m.input.ID)
}

// saveAndStop publishes guest memory and then stops Firecracker.
func (m *machine) saveAndStop(ctx context.Context) error {
	state, err := m.status(ctx)
	if err != nil {
		return err
	}

	// Recover a snapshot published before a daemon crash.
	if _, ok := m.savedState(); ok {
		switch state {
		case vm.StateStopped:
			return nil
		case vm.StatePaused:
			if err := m.kill(ctx); err != nil {
				return err
			}
			return nil
		default:
			return vm.ErrConflict
		}
	}

	pausedByThisCall := false
	switch state {
	case vm.StateRunning:
		if err := m.api.Pause(ctx); err != nil {
			return fmt.Errorf("pause before save: %w", err)
		}
		pausedByThisCall = true
	case vm.StatePaused:
		// An already paused guest needs no second pause.
	default:
		return vm.ErrConflict
	}

	_, err = m.runtime.createSavedState(ctx, m.input)
	if err != nil {
		return m.recoverFailedSave(ctx, pausedByThisCall, err)
	}

	if err := m.kill(ctx); err != nil {
		return err
	}

	return nil
}

// recoverFailedSave removes the pending snapshot and resumes only a guest
// paused by this call.
func (m *machine) recoverFailedSave(ctx context.Context, pausedByThisCall bool, cause error) error {
	recovery := []error{cause}
	if err := os.RemoveAll(m.runtime.configuration.pendingSavedStateDirectory(m.input.ID)); err != nil {
		recovery = append(recovery, fmt.Errorf("remove pending snapshot: %w", err))
	}
	if pausedByThisCall {
		if err := m.api.Resume(ctx); err != nil {
			recovery = append(recovery, fmt.Errorf("resume after failed save: %w", err))
		}
	}
	return errors.Join(recovery...)
}

// Pause halts the guest virtual CPUs.
func (m *machine) Pause(ctx context.Context) error {
	state, err := m.status(ctx)
	if err != nil {
		return err
	}
	if state != vm.StateRunning {
		return vm.ErrConflict
	}

	return m.api.Pause(ctx)
}

// Resume returns a paused guest to the running state.
func (m *machine) Resume(ctx context.Context) error {
	state, err := m.status(ctx)
	if err != nil {
		return err
	}
	if state != vm.StatePaused {
		return vm.ErrConflict
	}

	return m.api.Resume(ctx)
}

// kill stops a VM without giving the guest a chance to shut down.
func (m *machine) kill(ctx context.Context) error {
	if err := m.runtime.units.Kill(ctx, m.input.ID, syscall.SIGKILL); err != nil {
		// systemd can kill the main process and still fail to signal auxiliary processes.
		// A completed stop job confirms the whole unit has stopped before we reset it.
		if stopError := m.runtime.units.Stop(ctx, m.input.ID); stopError != nil {
			return errors.Join(err, fmt.Errorf("stop VM unit after kill failure: %w", stopError))
		}
	}
	if _, err := m.runtime.units.Wait(ctx, m.input.ID); err != nil {
		return err
	}
	_ = m.runtime.serialBroker.Close(m.input.ID)

	return m.runtime.units.ResetFailed(ctx, m.input.ID)
}

// shutdownGuest asks the guest to power off, then kills it after a timeout.
func (m *machine) shutdownGuest(ctx context.Context) error {
	if m.api.SendCtrlAltDel(ctx) == nil {
		wait, cancel := context.WithTimeout(ctx, m.stopTimeout)
		defer cancel()

		if _, err := m.runtime.units.Wait(wait, m.input.ID); err == nil {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}

	return m.kill(ctx)
}

// cleanupSystemd stops the unit and clears its state, so the ID can be reused.
func (m *machine) cleanupSystemd(ctx context.Context) error {
	if err := m.runtime.units.Stop(ctx, m.input.ID); err != nil {
		return fmt.Errorf("stop VM unit: %w", err)
	}
	_ = m.runtime.serialBroker.Close(m.input.ID)

	if err := m.runtime.units.ResetFailed(ctx, m.input.ID); err != nil {
		return fmt.Errorf("reset VM unit: %w", err)
	}

	return nil
}

// recordImageUse marks the image as used without failing a started VM.
func (m *machine) recordImageUse() {
	image := m.input.Specification.Image
	if m.input.Specification.Rescue.Enabled {
		image = m.input.Specification.Rescue.Image
	}
	if err := m.runtime.imageStore.RecordImageUse(image.Name, time.Now()); err != nil {
		m.runtime.logger.Error("record image use failed", "virtual_machine_id", m.input.ID, "error", err)
	}
}
