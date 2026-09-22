package vm

import (
	"context"
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

// EnsureMigrationNetwork creates and records the destination network for cold start.
func (host *MigrationHost) EnsureMigrationNetwork(ctx context.Context, virtualMachineID string) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	networkInterface, err := manager.network.Ensure(ctx, networkRequest(desired))
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
	return host.RestoreRuntimeState(ctx, virtualMachineID, desired.State)
}

// RestoreRuntimeState cold-starts running or paused VMs, verifies the result,
// and records state and generations. Stopped VMs stay stopped.
func (host *MigrationHost) RestoreRuntimeState(ctx context.Context, virtualMachineID string, desiredState State) error {
	manager := host.manager
	desired, observed, err := manager.newVirtualMachine(virtualMachineID).records()
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)

	resultState, err := manager.coldStartForState(ctx, desiredState, machine)
	if err != nil {
		return err
	}
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return err
	}
	if status.State != resultState {
		return fmt.Errorf("migrated VM %s is %s, want %s", virtualMachineID, status.State, resultState)
	}
	return manager.writeAppliedState(desired, &observed, resultState)
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
