package vm

import (
	"context"
	"errors"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// RestoreAfterTraffic records a wake request after new traffic.
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
	if event.Target != target || desired.State != StateRunning || desired.Specification.Rescue.Enabled || desired.Specification.SleepAfterIdleSeconds <= 0 {
		return nil
	}
	defer func() {
		if restoreError != nil {
			restoreError = errors.Join(restoreError, manager.traffic.StartWatching(target))
		}
	}()

	// A locked source keeps the request: its migration then fails at stop, and the VM wakes after unlock.
	if observed.SleepState == SleepStateAwake || desired.Generation > observed.Generation || manager.isDestinationReserved(desired.ID) {
		return nil
	}
	desired.Generation++
	if err := manager.store.writeDesired(desired); err != nil {
		return err
	}
	manager.stopSleepUpload(desired.ID)
	return nil
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
		desired.Specification.Rescue.Enabled || desired.Specification.SleepAfterIdleSeconds <= 0 ||
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
		return manager.saveAndStop(ctx, machine)
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

	return true, manager.writeAppliedState(desired, observed, StateStopped, SleepStateSleeping)
}

// resetIdle starts the idle time of a Sleepy VM again after it starts.
func (manager *Manager) resetIdle(desired DesiredRecord) {
	if manager.traffic == nil || desired.Specification.SleepAfterIdleSeconds <= 0 {
		return
	}
	target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
	if err := manager.traffic.ResetIdle(target); err != nil {
		manager.logTrafficError(desired.ID, err)
	}
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
