package vm

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// Phase names the host operation in progress. It is stored in the observed
// record so an operator can find where a pass stopped.
const (
	phaseInspect        = "inspect"
	phaseNetwork        = "network"
	phaseStorage        = "storage"
	phaseStart          = "start"
	phaseStop           = "stop"
	phasePause          = "pause"
	phaseResume         = "resume"
	phaseRestart        = "restart"
	phaseMetadata       = "metadata"
	phaseDisk           = "disk"
	phaseSnapshot       = "snapshot"
	phaseSaveState      = "save-state"
	phaseRestoreState   = "restore-state"
	phaseDestroyRuntime = "destroy-runtime"
	phaseDestroyNetwork = "destroy-network"
	phaseDestroyStorage = "destroy-storage"
)

// Reconcile moves observed VM state toward the latest desired record.
func (manager *Manager) Reconcile(ctx context.Context, identifier string) error {
	virtualMachine := manager.newVirtualMachine(identifier)
	unlock, err := virtualMachine.lock(ctx)
	if err != nil {
		return err
	}
	defer unlock()

	// Migration sources and incoming destinations pause normal reconciliation.
	if manager.isMigrating(identifier) {
		return nil
	}

	desired, observed, err := virtualMachine.records()
	if err != nil {
		return err
	}
	operationID := newOperationID()

	if desired.State == StateDestroyed {
		return manager.reconcileDestroyed(ctx, desired, observed, operationID)
	}

	return manager.reconcileActive(ctx, desired, observed, operationID)
}

// reconcileActive applies network, restart, power, shape, and disk changes.
func (manager *Manager) reconcileActive(
	ctx context.Context,
	desired DesiredRecord,
	observed ObservedRecord,
	operationID string,
) error {
	var networkInterface NetworkInterface
	err := manager.runOperation(ctx, desired.ID, &observed, operationID, phaseNetwork, func() error {
		var ensureError error
		networkInterface, ensureError = manager.network.Ensure(ctx, networkRequest(desired))
		return ensureError
	})
	if err != nil {
		return err
	}

	observed.NetworkInterface = networkInterface
	machine := runtimeMachine(desired, networkInterface)

	if desired.State == StateStopped {
		return manager.reconcileHardStop(ctx, desired, machine, &observed, operationID)
	}

	if desired.Specification.Rescue.Enabled || desired.SpecificationGeneration > observed.SpecificationGeneration ||
		desired.RestartGeneration > observed.RestartGeneration ||
		desired.RescueGeneration > observed.RescueGeneration {
		if err := manager.runOperation(ctx, desired.ID, &observed, operationID, phaseStop, func() error {
			return manager.runtime.DeleteSavedState(ctx, machine)
		}); err != nil {
			return err
		}
	}

	status, err := manager.inspect(ctx, desired.ID, machine, &observed, operationID)
	if err != nil {
		return err
	}
	if desired.Specification.Rescue.Enabled && status.RescueRebootRequested &&
		(status.State == StateStopped || status.State == StateFailed) {
		desired.Specification.Rescue = Rescue{}
		desired.RescueGeneration++
		desired.Generation++
		if err := manager.store.writeDesired(desired); err != nil {
			return err
		}
		machine = runtimeMachine(desired, networkInterface)
	}
	status, reconcileComplete, err := manager.reconcileSavedState(ctx, desired, machine, &observed, operationID, status)
	if err != nil || reconcileComplete {
		return err
	}

	if observed.RestartGeneration < desired.RestartGeneration || observed.RescueGeneration < desired.RescueGeneration {
		status, err = manager.applyRestart(ctx, desired.ID, machine, status, &observed, operationID)
		if err != nil {
			return err
		}
	}
	if desired.RescueGeneration > observed.RescueGeneration && !desired.Specification.Rescue.Enabled {
		if err := manager.runOperation(ctx, desired.ID, &observed, operationID, phaseStorage, func() error {
			return manager.storage.ReleaseRescueDisk(ctx, desired.ID)
		}); err != nil {
			return err
		}
	}

	if observed.RestartGeneration < desired.RestartGeneration || observed.RescueGeneration < desired.RescueGeneration {
		observed.RestartGeneration = desired.RestartGeneration
		observed.RescueGeneration = desired.RescueGeneration
	}

	status, err = manager.applyDesiredState(ctx, desired.ID, machine, status, desired.State, &observed, operationID)
	if err != nil {
		return err
	}
	if desired.Generation != observed.Generation {
		if err := manager.applyDesiredSpecification(ctx, desired, machine, status, &observed, operationID); err != nil {
			return err
		}
		observed.Generation = desired.Generation
		observed.SpecificationGeneration = desired.SpecificationGeneration
	}
	if err := manager.runOperation(ctx, desired.ID, &observed, operationID, phaseStorage, func() error {
		var usageError error
		observed.Disk, usageError = manager.storage.DiskUsage(ctx, desired.ID)
		return usageError
	}); err != nil {
		return err
	}

	stopped, err := manager.stopAfterIdle(ctx, desired, machine, status, &observed, operationID)
	if err != nil || stopped {
		return err
	}

	return manager.writeAppliedState(desired, &observed, status.State)
}

// reconcileSavedState applies the desired state when saved state is present.
func (manager *Manager) reconcileSavedState(
	ctx context.Context,
	desired DesiredRecord,
	machine RuntimeMachine,
	observed *ObservedRecord,
	operationID string,
	status RuntimeStatus,
) (RuntimeStatus, bool, error) {
	if !status.HasSavedState {
		return status, false, nil
	}

	if status.State == StateRunning {
		if err := manager.runtime.DeleteSavedState(ctx, machine); err != nil {
			return status, false, fmt.Errorf("delete stale saved state for VM %s: %w", desired.ID, err)
		}
		manager.stopTrafficWatch(traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID})
		status.HasSavedState = false
		return status, false, nil
	}

	if status.State == StatePaused {
		if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseSaveState, func() error {
			return manager.runtime.SaveAndStop(ctx, machine)
		}); err != nil {
			return status, false, err
		}
		status.State = StateStopped
	}

	if status.State != StateStopped {
		return status, false, &TransitionError{DesiredState: desired.State, ObservedState: status.State}
	}

	switch desired.State {
	case StateRunning:
		if !desired.Specification.Rescue.Enabled && desired.Specification.SleepAfterIdleSeconds > 0 && manager.traffic != nil {
			target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
			if err := manager.traffic.StartWatching(target); err != nil {
				return status, false, fmt.Errorf("watch traffic for VM %s: %w", desired.ID, err)
			}
			return status, true, manager.writeAppliedState(desired, observed, StateStopped)
		}
		if err := manager.restoreSavedState(ctx, desired, machine, observed, operationID, StateRunning); err != nil {
			return status, false, err
		}
		return RuntimeStatus{State: StateRunning}, false, nil
	case StatePaused:
		if err := manager.restoreSavedState(ctx, desired, machine, observed, operationID, StatePaused); err != nil {
			return status, false, err
		}
		return RuntimeStatus{State: StatePaused}, false, nil
	default:
		return status, false, &TransitionError{DesiredState: desired.State, ObservedState: status.State}
	}
}

// restoreSavedState restores a saved runtime and records its resulting state.
func (manager *Manager) restoreSavedState(
	ctx context.Context,
	desired DesiredRecord,
	machine RuntimeMachine,
	observed *ObservedRecord,
	operationID string,
	resultState State,
) error {
	restore := manager.runtime.Restore
	if resultState == StatePaused {
		restore = manager.runtime.RestorePaused
	}
	if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseRestoreState, func() error {
		return restore(ctx, machine)
	}); err != nil {
		return err
	}
	manager.stopTrafficWatch(traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID})
	return manager.writeAppliedState(desired, observed, resultState)
}

// reconcileHardStop stops the runtime and applies all stopped-state changes.
func (manager *Manager) reconcileHardStop(
	ctx context.Context,
	desired DesiredRecord,
	machine RuntimeMachine,
	observed *ObservedRecord,
	operationID string,
) error {
	manager.stopTrafficWatch(traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID})
	if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseStop, func() error {
		return manager.runtime.Stop(ctx, machine)
	}); err != nil {
		return err
	}
	if desired.RescueGeneration > observed.RescueGeneration && !desired.Specification.Rescue.Enabled {
		if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseStorage, func() error {
			return manager.storage.ReleaseRescueDisk(ctx, desired.ID)
		}); err != nil {
			return err
		}
	}

	if err := manager.applyDesiredSpecification(ctx, desired, machine, RuntimeStatus{State: StateStopped}, observed, operationID); err != nil {
		return err
	}
	return manager.writeAppliedState(desired, observed, StateStopped)
}

// writeAppliedState records a complete state transition at the desired generations.
func (manager *Manager) writeAppliedState(desired DesiredRecord, observed *ObservedRecord, state State) error {
	rescueChanged := observed.RescueEnabled != desired.Specification.Rescue.Enabled || observed.RescueGeneration != desired.RescueGeneration
	observed.State = state
	observed.Generation = desired.Generation
	observed.SpecificationGeneration = desired.SpecificationGeneration
	observed.RestartGeneration = desired.RestartGeneration
	observed.RescueGeneration = desired.RescueGeneration
	observed.RescueEnabled = desired.Specification.Rescue.Enabled
	observed.completeOperation()
	if err := manager.store.writeObserved(desired.ID, *observed); err != nil {
		return err
	}
	if rescueChanged {
		manager.logger.Info("rescue mode applied", "vm_id", desired.ID, "rescue_enabled", observed.RescueEnabled, "rescue_generation", desired.RescueGeneration, "state", state)
	}
	return nil
}

// inspect reads runtime state and stores StateUnknown on failure.
func (manager *Manager) inspect(
	ctx context.Context,
	identifier string,
	machine RuntimeMachine,
	observed *ObservedRecord,
	operationID string,
) (RuntimeStatus, error) {
	var status RuntimeStatus
	err := manager.runOperation(ctx, identifier, observed, operationID, phaseInspect, func() error {
		var inspectError error
		status, inspectError = manager.runtime.Inspect(ctx, machine)
		return inspectError
	})
	if err != nil {
		observed.State = StateUnknown
		_ = manager.store.writeObserved(identifier, *observed)
	}
	return status, err
}

// applyRestart stops the old guest before storage cleanup and the next boot.
func (manager *Manager) applyRestart(
	ctx context.Context,
	identifier string,
	machine RuntimeMachine,
	status RuntimeStatus,
	observed *ObservedRecord,
	operationID string,
) (RuntimeStatus, error) {
	if status.State == StateStopped {
		return status, nil
	}
	err := manager.runOperation(ctx, identifier, observed, operationID, phaseRestart, func() error {
		return manager.runtime.Stop(ctx, machine)
	})
	if err != nil {
		return status, err
	}
	return RuntimeStatus{State: StateStopped}, nil
}

// applyDesiredState moves the runtime to the requested power state.
func (manager *Manager) applyDesiredState(
	ctx context.Context,
	identifier string,
	machine RuntimeMachine,
	status RuntimeStatus,
	desiredState State,
	observed *ObservedRecord,
	operationID string,
) (RuntimeStatus, error) {
	switch desiredState {
	case StateRunning:
		if status.State == StatePaused {
			return manager.runRuntimeTransition(ctx, identifier, machine, observed, operationID, phaseResume, StateRunning, manager.runtime.Resume)
		}
		if status.State != StateRunning {
			return manager.runRuntimeTransition(ctx, identifier, machine, observed, operationID, phaseStart, StateRunning, manager.runtime.Start)
		}
	case StatePaused:
		if status.State != StateRunning && status.State != StatePaused {
			if _, err := manager.runRuntimeTransition(ctx, identifier, machine, observed, operationID, phaseStart, StateRunning, manager.runtime.Start); err != nil {
				return status, err
			}
			status.State = StateRunning
		}
		if status.State == StateRunning {
			return manager.runRuntimeTransition(ctx, identifier, machine, observed, operationID, phasePause, StatePaused, manager.runtime.Pause)
		}
	default:
		return status, &TransitionError{DesiredState: desiredState, ObservedState: status.State}
	}
	return status, nil
}

// runRuntimeTransition runs one runtime call and reports the state it produces.
func (manager *Manager) runRuntimeTransition(
	ctx context.Context,
	identifier string,
	machine RuntimeMachine,
	observed *ObservedRecord,
	operationID string,
	phase string,
	resultState State,
	operation func(context.Context, RuntimeMachine) error,
) (RuntimeStatus, error) {
	status := RuntimeStatus{State: resultState}
	err := manager.runOperation(ctx, identifier, observed, operationID, phase, func() error {
		operationError := operation(ctx, machine)
		if operationError == nil {
			return nil
		}

		var inspectError error
		status, inspectError = manager.runtime.Inspect(ctx, machine)
		if inspectError != nil {
			status = RuntimeStatus{State: StateUnknown}
			operationError = errors.Join(operationError, fmt.Errorf("inspect failed transition: %w", inspectError))
		}
		observed.State = status.State
		return operationError
	})
	return status, err
}

// applyDesiredSpecification applies specification changes.
func (manager *Manager) applyDesiredSpecification(
	ctx context.Context,
	desired DesiredRecord,
	machine RuntimeMachine,
	status RuntimeStatus,
	observed *ObservedRecord,
	operationID string,
) error {
	if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseStorage, func() error {
		return manager.storage.ResizeDisk(ctx, desired.ID, desired.Specification.DiskMiB)
	}); err != nil {
		return err
	}
	if status.State == StateRunning || status.State == StatePaused {
		if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseDisk, func() error {
			return manager.runtime.RefreshDisk(ctx, machine)
		}); err != nil {
			return err
		}
		if err := manager.runOperation(ctx, desired.ID, observed, operationID, phaseMetadata, func() error {
			return manager.runtime.RefreshMetadata(ctx, machine)
		}); err != nil {
			return err
		}
	}
	return nil
}

// reconcileDestroyed releases host resources in order and records each completed step.
func (manager *Manager) reconcileDestroyed(
	ctx context.Context,
	desired DesiredRecord,
	observed ObservedRecord,
	operationID string,
) error {
	machine := runtimeMachine(desired, NetworkInterface{})
	steps := []struct {
		complete *bool
		phase    string
		run      func() error
	}{
		{&observed.RuntimeCleanupComplete, phaseDestroyRuntime, func() error { return manager.runtime.Remove(ctx, machine) }},
		{&observed.NetworkCleanupComplete, phaseDestroyNetwork, func() error {
			return manager.network.Release(ctx, NetworkReleaseRequest{
				VirtualMachineID:  desired.ID,
				UserID:            desired.UserID,
				WireGuardMeshIPv6: desired.Specification.Network.WireGuardMeshIPv6,
			})
		}},
		{&observed.StorageCleanupComplete, phaseDestroyStorage, func() error { return manager.storage.Release(ctx, desired.ID) }},
	}
	for _, step := range steps {
		if *step.complete {
			continue
		}
		if err := manager.runOperation(ctx, desired.ID, &observed, operationID, step.phase, step.run); err != nil {
			return err
		}
		*step.complete = true
		observed.State = StateDestroyed
		if err := manager.store.writeObserved(desired.ID, observed); err != nil {
			return err
		}
	}
	if err := manager.store.remove(desired.ID); err != nil {
		return fmt.Errorf("remove virtual machine records: %w", err)
	}
	return nil
}

// runOperation stores the phase before a host call and records any failure.
func (manager *Manager) runOperation(
	ctx context.Context,
	identifier string,
	observed *ObservedRecord,
	operationID string,
	phase string,
	operation func() error,
) error {
	now := time.Now().UTC()
	observed.Phase = phase
	if observed.OperationID != operationID {
		observed.OperationStartedAt = now
	}
	observed.OperationID = operationID
	observed.UpdatedAt = now
	observed.Error = nil
	if err := manager.store.writeObserved(identifier, *observed); err != nil {
		manager.logOperationFailure(ctx, identifier, observed, phase, now, err)
		return err
	}
	if err := operation(); err != nil {
		observed.Error = &OperationError{
			Code: "operation_failed", Message: phase + " operation failed",
			LocalDetail: err.Error(), UpdatedAt: time.Now().UTC(),
		}
		observed.UpdatedAt = observed.Error.UpdatedAt
		if writeError := manager.store.writeObserved(identifier, *observed); writeError != nil {
			manager.logOperationFailure(ctx, identifier, observed, phase, now, errors.Join(err, writeError))
			return fmt.Errorf("%s: %w", phase, writeError)
		}
		manager.logOperationFailure(ctx, identifier, observed, phase, now, err)
		return fmt.Errorf("%s: %w", phase, err)
	}
	manager.logger.Info("virtual machine operation completed",
		"component", "vm", "operation", "reconcile", "phase", phase, "vm_id", identifier,
		"operation_id", operationID, "applied_generation", observed.Generation,
		"observed_state", observed.State, "duration", time.Since(now),
	)
	return nil
}

// logOperationFailure reports a failed host operation with its correlation data.
func (manager *Manager) logOperationFailure(
	ctx context.Context,
	identifier string,
	observed *ObservedRecord,
	phase string,
	startedAt time.Time,
	err error,
) {
	manager.logger.ErrorContext(ctx, "virtual machine operation failed",
		"component", "vm", "operation", "reconcile", "phase", phase, "vm_id", identifier,
		"operation_id", observed.OperationID, "applied_generation", observed.Generation,
		"observed_state", observed.State, "duration", time.Since(startedAt), "error", err,
		"rescue_generation", observed.RescueGeneration, "rescue_enabled", observed.RescueEnabled,
	)
}
