package vm

import (
	"context"
	"errors"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// RestoreAfterTraffic restores a stopped Sleepy VM after new traffic.
func (manager *Manager) RestoreAfterTraffic(ctx context.Context, event traffic.Event) (restoreError error) {
	if manager.traffic == nil {
		return nil
	}
	virtualMachine := manager.newVirtualMachine(event.Target.VirtualMachineID)
	unlock, err := virtualMachine.lock(ctx)
	if err != nil {
		return err
	}
	defer unlock()

	desired, observed, err := virtualMachine.records()
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
	if event.Target != target || desired.State != StateRunning || desired.Specification.SleepAfterIdleSeconds <= 0 {
		return nil
	}
	defer func() {
		if restoreError != nil {
			restoreError = errors.Join(restoreError, manager.traffic.StartWatching(target))
		}
	}()

	return manager.restoreSleeping(ctx, desired, observed)
}

// wakeSleepingVirtualMachine restores a Sleepy VM that sleeps with saved state.
// It does nothing for any other VM.
func (manager *Manager) wakeSleepingVirtualMachine(ctx context.Context, identifier string) error {
	if manager.traffic == nil {
		return nil
	}
	virtualMachine := manager.newVirtualMachine(identifier)
	unlock, err := virtualMachine.lock(ctx)
	if err != nil {
		return err
	}
	defer unlock()

	desired, observed, err := virtualMachine.records()
	if err != nil {
		return err
	}
	if desired.State != StateRunning || desired.Specification.SleepAfterIdleSeconds <= 0 {
		return nil
	}
	return manager.restoreSleeping(ctx, desired, observed)
}

// restoreSleeping restores the saved state of a stopped Sleepy VM.
func (manager *Manager) restoreSleeping(ctx context.Context, desired DesiredRecord, observed ObservedRecord) error {
	networkInterface, err := manager.network.Ensure(ctx, manager.networkRequest(desired))
	if err != nil {
		return err
	}
	machine := runtimeMachine(desired, networkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return err
	}
	if status.State != StateStopped || !status.HasSavedState {
		return nil
	}

	return manager.restoreSavedState(ctx, desired, machine, &observed, newOperationID(), StateRunning)
}

// stopAfterIdle saves and stops a Sleepy VM when network traffic remains idle.
func (manager *Manager) stopAfterIdle(
	ctx context.Context,
	desired DesiredRecord,
	machine RuntimeMachine,
	status RuntimeStatus,
	observed *ObservedRecord,
	operationID string,
) (bool, error) {
	if manager.traffic == nil ||
		desired.State != StateRunning ||
		desired.Specification.SleepAfterIdleSeconds <= 0 ||
		status.State != StateRunning ||
		observed.Generation != desired.Generation ||
		observed.SpecificationGeneration != desired.SpecificationGeneration ||
		observed.RestartGeneration != desired.RestartGeneration ||
		observed.Error != nil {
		return false, nil
	}

	target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
	initialSample, err := manager.traffic.Sample(target)
	if err != nil {
		manager.logTrafficError(desired.ID, err)
		return false, nil
	}
	idleTimeout := time.Duration(desired.Specification.SleepAfterIdleSeconds) * time.Second
	if initialSample.IdleFor < idleTimeout {
		return false, nil
	}

	if err := manager.traffic.StartWatching(target); err != nil {
		manager.logTrafficError(desired.ID, err)
		return false, nil
	}
	watchedSample, err := manager.traffic.Sample(target)
	if err != nil || watchedSample.PacketSequence != initialSample.PacketSequence || watchedSample.IdleFor < idleTimeout {
		manager.stopTrafficWatch(target)
		if err != nil {
			manager.logTrafficError(desired.ID, err)
		}
		return false, nil
	}

	if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseSaveState, func() error {
		return manager.runtime.SaveAndStop(ctx, machine)
	}); err != nil {
		manager.stopTrafficWatch(target)
		return false, err
	}

	stoppedSample, sampleError := manager.traffic.Sample(target)
	if sampleError != nil || stoppedSample.PacketSequence != watchedSample.PacketSequence {
		if sampleError != nil {
			manager.logTrafficError(desired.ID, sampleError)
		}
		if err := manager.restoreSavedState(ctx, desired, machine, observed, operationID, StateRunning); err != nil {
			return false, err
		}
		return false, nil
	}

	return true, manager.writeAppliedState(desired, observed, StateStopped)
}

// stopTrafficWatch disables packet events and logs a failure.
func (manager *Manager) stopTrafficWatch(target traffic.Target) {
	if manager.traffic == nil {
		return
	}
	if err := manager.traffic.StopWatching(target); err != nil {
		manager.logTrafficError(target.VirtualMachineID, err)
	}
}

// logTrafficError records an idle watcher traffic monitoring failure.
func (manager *Manager) logTrafficError(virtualMachineID string, err error) {
	manager.logger.Warn("network traffic operation failed", "virtual_machine_id", virtualMachineID, "error", err)
}
