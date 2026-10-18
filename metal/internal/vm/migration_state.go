package vm

import (
	"context"
	"errors"
	"fmt"
)

// The VM manager owns migration runtime and network work. Callers hold the VM
// operation lock, so these methods do not lock again.

// NormalizeSourceToStopped brings the source to a clean stopped state. Saved
// guest memory is restored and then stopped, not transferred.
func (host *MigrationHost) NormalizeSourceToStopped(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return fmt.Errorf("inspect migration source %s: %w", virtualMachineID, err)
	}

	switch status.State {
	case StateRunning, StateCreated:
		return manager.runtime.Stop(ctx, machine)
	case StatePaused:
		if err := manager.runtime.Resume(ctx, machine); err != nil {
			return err
		}
		return manager.runtime.Stop(ctx, machine)
	case StateStopped:
		if !status.HasSavedState {
			return nil
		}
		if err := manager.runtime.Restore(ctx, machine); err != nil {
			return err
		}
		return manager.runtime.Stop(ctx, machine)
	default:
		return &TransitionError{DesiredState: StateStopped, ObservedState: status.State}
	}
}

// SaveAndStopSource saves guest memory before terminating the source process.
func (host *MigrationHost) SaveAndStopSource(ctx context.Context, virtualMachineID string) error {
	desired, observed, err := host.manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	return host.manager.runtime.SaveAndStop(ctx, runtimeMachine(desired, observed.NetworkInterface))
}

// LimitSourceDisk applies a temporary combined read and write limit before a
// stream. It never raises the configured limit.
func (host *MigrationHost) LimitSourceDisk(ctx context.Context, virtualMachineID string, throughputMiBps int) (int, error) {
	manager := host.manager
	if throughputMiBps <= 0 {
		return 0, nil
	}
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return 0, err
	}
	if configured := desired.Specification.Disk.ThroughputMiBps; configured > 0 && configured < throughputMiBps {
		throughputMiBps = configured
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	machine.Specification.Disk.ThroughputMiBps = throughputMiBps
	if err := manager.runtime.RefreshDisk(ctx, machine); err != nil {
		return 0, err
	}
	return throughputMiBps, nil
}

// RemoveMigrationNetwork releases the source network before destination setup.
func (host *MigrationHost) RemoveMigrationNetwork(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, err := manager.store.readDesired(virtualMachineID)
	if err != nil {
		return err
	}
	return manager.network.Release(ctx, NetworkReleaseRequest{
		VirtualMachineID:  desired.ID,
		UserID:            desired.UserID,
		WireGuardMeshIPv6: desired.Specification.Network.WireGuardMeshIPv6,
	})
}

// EnsureMigrationNetwork creates and records the destination network.
func (host *MigrationHost) EnsureMigrationNetwork(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	networkInterface, err := manager.network.Ensure(ctx, manager.networkRequest(desired))
	if err != nil {
		return err
	}
	observed.NetworkInterface = networkInterface
	return manager.store.writeObserved(virtualMachineID, observed)
}

// ApplyMigratedDestinationState grows the received disk to a resized shape, then
// applies the original state with a cold start.
func (host *MigrationHost) ApplyMigratedDestinationState(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, err := manager.store.readDesired(virtualMachineID)
	if err != nil {
		return err
	}
	if err := manager.storage.ResizeDisk(ctx, virtualMachineID, desired.Specification.DiskMiB); err != nil {
		return fmt.Errorf("grow migrated disk %s: %w", virtualMachineID, err)
	}
	return host.RestoreRuntimeState(ctx, virtualMachineID, desired.State, false)
}

// RestoreMigratedMemorySnapshot loads the incoming snapshot without running the guest.
func (host *MigrationHost) RestoreMigratedMemorySnapshot(ctx context.Context, virtualMachineID, directory string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return err
	}
	if status.State == StateRunning {
		return fmt.Errorf("migration destination already runs: %w", ErrConflict)
	}
	if status.State != StatePaused {
		if err := manager.runtime.RestorePausedFrom(ctx, machine, directory); err != nil {
			return err
		}
	}
	if err := manager.runtime.RefreshMetadata(ctx, machine); err != nil {
		return err
	}
	if err := manager.runtime.RefreshDisk(ctx, machine); err != nil {
		return err
	}
	return manager.recordMigrationRuntimeState(ctx, desired, &observed, StatePaused)
}

// ActivateMigratedDestination applies the desired power state after Atlas commits ownership.
func (host *MigrationHost) ActivateMigratedDestination(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return err
	}
	if status.State != desired.State {
		switch {
		case desired.State == StateRunning && status.State == StatePaused:
			if err = manager.runtime.Resume(ctx, machine); err != nil {
				err = manager.recoverFailedResume(ctx, machine, err)
			}
		case desired.State == StatePaused && status.State == StateRunning:
			err = manager.runtime.Pause(ctx, machine)
		case desired.State == StateStopped:
			err = manager.runtime.Remove(ctx, machine)
		case (status.State == StateStopped || status.State == StateFailed) &&
			(observed.State == StateRunning || observed.State == StatePaused):
			// A lost process can have changed the disk after its memory snapshot.
			_, err = manager.coldStartForState(ctx, desired.State, machine)
		case status.State == StateCreated:
			// A failed cold boot can leave a process that never started the guest.
			_, err = manager.coldRestartForState(ctx, desired.State, machine,
				&TransitionError{DesiredState: desired.State, ObservedState: status.State})
		default:
			return &TransitionError{DesiredState: desired.State, ObservedState: status.State}
		}
		if err != nil {
			return err
		}
	}
	if desired.State == StateRunning {
		if err := manager.runtime.CheckMigratedGuestSSH(ctx, machine); err != nil {
			return fmt.Errorf("check migrated guest: %w", err)
		}
	}
	return manager.recordMigrationRuntimeState(ctx, desired, &observed, desired.State)
}

// RestoreRuntimeState starts or restores running or paused VMs, verifies the result,
// and records state and generations. Stopped VMs stay stopped.
func (host *MigrationHost) RestoreRuntimeState(ctx context.Context, virtualMachineID string, desiredState State, fromSavedState bool) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)

	resultState := desiredState
	if fromSavedState {
		status, inspectError := manager.runtime.Inspect(ctx, machine)
		if inspectError != nil {
			return inspectError
		}
		switch {
		case status.State == desiredState:
		case desiredState == StatePaused && status.State == StateRunning:
			// An earlier cold boot ran the guest, so only its pause remains.
			err = manager.runtime.Pause(ctx, machine)
		case desiredState == StateRunning || desiredState == StatePaused:
			if desiredState == StateRunning {
				err = manager.runtime.Restore(ctx, machine)
			} else {
				err = manager.runtime.RestorePaused(ctx, machine)
			}
			if err != nil {
				resultState, err = manager.recoverFailedRestore(ctx, desiredState, machine, err)
			}
		default:
			return &TransitionError{DesiredState: desiredState, ObservedState: status.State}
		}
	} else {
		resultState, err = manager.coldStartForState(ctx, desiredState, machine)
	}
	if err != nil {
		return err
	}
	return manager.recordMigrationRuntimeState(ctx, desired, &observed, resultState)
}

func (manager *Manager) recordMigrationRuntimeState(ctx context.Context, desired DesiredRecord, observed *ObservedRecord, state State) error {
	status, err := manager.runtime.Inspect(ctx, runtimeMachine(desired, observed.NetworkInterface))
	if err != nil {
		return err
	}
	if status.State != state {
		return fmt.Errorf("migrated VM %s is %s, want %s", desired.ID, status.State, state)
	}
	return manager.writeAppliedState(desired, observed, state)
}

// RemoveMigratedRuntime removes migration runtime, jail, and network. Repeats
// are safe.
func (host *MigrationHost) RemoveMigratedRuntime(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, err := manager.store.readDesired(virtualMachineID)
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, NetworkInterface{})
	if err := manager.runtime.Remove(ctx, machine); err != nil {
		return err
	}
	return manager.network.Release(ctx, NetworkReleaseRequest{
		VirtualMachineID:  desired.ID,
		UserID:            desired.UserID,
		WireGuardMeshIPv6: desired.Specification.Network.WireGuardMeshIPv6,
	})
}

// RefreshSourceDisk reapplies the configured limit after an abort. It does
// nothing for stopped or unknown VMs.
func (host *MigrationHost) RefreshSourceDisk(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	return manager.runtime.RefreshDisk(ctx, runtimeMachine(desired, observed.NetworkInterface))
}

// recoverFailedResume inspects the guest after a resume error. A guest that resumed despite the
// error keeps its memory, and a still-paused guest retries the resume on the next pass. Only a
// guest whose process is gone cold-boots.
func (manager *Manager) recoverFailedResume(ctx context.Context, machine RuntimeMachine, cause error) error {
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return errors.Join(cause, err)
	}
	switch status.State {
	case StateRunning:
		return nil
	case StatePaused:
		return cause
	default:
		_, err = manager.coldRestartForState(ctx, StateRunning, machine, cause)
		return err
	}
}

// recoverFailedRestore inspects the guest after a restore error. A guest that reached the desired
// state keeps its memory; any other guest cold-boots.
func (manager *Manager) recoverFailedRestore(ctx context.Context, desiredState State, machine RuntimeMachine, cause error) (State, error) {
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return "", errors.Join(cause, err)
	}
	if status.State == desiredState {
		return desiredState, nil
	}
	return manager.coldRestartForState(ctx, desiredState, machine, cause)
}

// coldRestartForState discards guest memory that cannot resume and boots the current disk.
// The disk stays consistent; only the guest RAM state is lost.
func (manager *Manager) coldRestartForState(ctx context.Context, desiredState State, machine RuntimeMachine, cause error) (State, error) {
	manager.logger.WarnContext(ctx, "cold booting a virtual machine whose memory cannot resume",
		"virtual_machine_id", machine.ID, "error", cause)
	if err := manager.runtime.Remove(ctx, machine); err != nil {
		return "", errors.Join(cause, err)
	}
	if err := manager.runtime.DeleteSavedState(ctx, machine); err != nil {
		return "", errors.Join(cause, err)
	}
	return manager.coldStartForState(ctx, desiredState, machine)
}

// coldStartForState cold-starts the disk for the desired state.
func (manager *Manager) coldStartForState(ctx context.Context, desiredState State, machine RuntimeMachine) (State, error) {
	switch desiredState {
	case StateStopped:
		return StateStopped, nil
	case StateRunning:
		if err := manager.runtime.ColdStart(ctx, machine); err != nil {
			return "", err
		}
		return StateRunning, nil
	case StatePaused:
		if err := manager.runtime.ColdStart(ctx, machine); err != nil {
			return "", err
		}
		if err := manager.runtime.Pause(ctx, machine); err != nil {
			return "", err
		}
		return StatePaused, nil
	default:
		return "", &TransitionError{DesiredState: desiredState, ObservedState: StateUnknown}
	}
}
