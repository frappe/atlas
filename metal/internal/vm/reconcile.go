package vm

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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

func isRoutinePhase(phase string) bool {
	return phase == phaseInspect || phase == phaseNetwork || phase == phaseStorage
}

// Reconcile moves observed VM state toward the latest desired record.
func (manager *Manager) Reconcile(ctx context.Context, identifier string) error {
	return manager.reconcile(ctx, identifier, false)
}

// RestoreSleepState reconciles sleeping VMs outside the normal VM worker pool.
func (manager *Manager) RestoreSleepState(ctx context.Context, identifier string) error {
	return manager.reconcile(ctx, identifier, true)
}

func (manager *Manager) reconcile(ctx context.Context, identifier string, sleeping bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	virtualMachine := manager.newVirtualMachine(identifier)
	unlock, acquired := manager.operationLocks.TryLock(identifier)
	if !acquired {
		return nil
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
	if (observed.SleepState != SleepStateAwake) != sleeping {
		return nil
	}
	return manager.reconcileRecords(ctx, desired, observed)
}

func (manager *Manager) reconcileRecords(ctx context.Context, desired DesiredRecord, observed ObservedRecord) error {
	if pending, err := manager.hasPendingSleepTransfer(desired, observed); err != nil {
		return err
	} else if pending {
		return manager.maintainSleepTransfer(ctx, desired, observed)
	}
	operationID := newOperationID()
	acceptedAt := manager.desiredWrittenAt(desired.ID)

	var err error
	if desired.State == StateDestroyed {
		err = manager.reconcileDestroyed(ctx, desired, observed, operationID)
	} else {
		err = manager.reconcileActive(ctx, desired, observed, operationID)
	}
	if err == nil {
		manager.recordAction(desired, observed, acceptedAt)
	}
	return err
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
		networkInterface, ensureError = manager.network.Ensure(ctx, manager.networkRequest(desired))
		return ensureError
	})
	if err != nil {
		return err
	}

	observed.NetworkInterface = networkInterface
	machine := runtimeMachine(desired, networkInterface)
	discardSavedState := desired.State == StateStopped || desired.SpecificationGeneration > observed.SpecificationGeneration ||
		desired.RestartGeneration > observed.RestartGeneration
	if discardSavedState && observed.SleepState == SleepStateComatose {
		if err := manager.runOperation(ctx, desired.ID, &observed, operationID, phaseStorage, func() error {
			record, err := manager.readSleepRecord(desired.ID)
			if err != nil {
				return err
			}
			return manager.restoreSleepDisk(ctx, desired.ID, record)
		}); err != nil {
			return err
		}
	}

	if desired.State == StateStopped {
		return manager.reconcileHardStop(ctx, desired, machine, &observed, operationID)
	}

	if desired.SpecificationGeneration > observed.SpecificationGeneration ||
		desired.RestartGeneration > observed.RestartGeneration {
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
	status, reconcileComplete, err := manager.reconcileSavedState(ctx, desired, machine, &observed, operationID, status)
	if err != nil || reconcileComplete {
		return err
	}

	if observed.RestartGeneration < desired.RestartGeneration {
		status, err = manager.applyRestart(ctx, desired.ID, machine, status, &observed, operationID)
		if err != nil {
			return err
		}
		observed.RestartGeneration = desired.RestartGeneration
	}

	previousState := status.State
	status, err = manager.applyDesiredState(ctx, desired.ID, machine, status, desired.State, &observed, operationID)
	if err != nil {
		return err
	}
	if previousState != StateRunning && status.State == StateRunning {
		manager.resetIdle(desired)
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

	observed.State = status.State
	observed.SleepState = SleepStateAwake
	observed.completeOperation()

	return manager.store.writeObserved(desired.ID, observed)
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
	hasRemoteState := observed.hasRemoteSleepState() &&
		desired.SpecificationGeneration == observed.SpecificationGeneration && desired.RestartGeneration == observed.RestartGeneration
	if !status.HasSavedState && !hasRemoteState {
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
		if desired.Specification.SleepAfterIdleSeconds > 0 && manager.traffic != nil && desired.Generation == observed.Generation {
			target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
			if err := manager.traffic.StartWatching(target); err != nil {
				return status, false, fmt.Errorf("watch traffic for VM %s: %w", desired.ID, err)
			}
			sleepState := observed.SleepState
			if !hasRemoteState {
				sleepState = SleepStateSleeping
			}
			return status, true, manager.writeAppliedState(desired, observed, StateStopped, sleepState)
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
		if err := manager.restoreSleepArtifacts(ctx, desired, machine, *observed); err != nil {
			return err
		}
		if err := restore(ctx, machine); err != nil {
			return manager.recoverSleepRestore(ctx, machine, observed, resultState, err)
		}
		return nil
	}); err != nil {
		if resultState == StateRunning && manager.traffic != nil && desired.Specification.SleepAfterIdleSeconds > 0 {
			err = errors.Join(err, manager.traffic.StartWatching(traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}))
		}
		return err
	}
	if resultState == StateRunning {
		manager.resetIdle(desired)
	}
	manager.stopTrafficWatch(traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID})
	return manager.writeAppliedState(desired, observed, resultState, SleepStateAwake)
}

func (manager *Manager) recoverSleepRestore(ctx context.Context, machine RuntimeMachine, observed *ObservedRecord, resultState State, cause error) error {
	if ctx.Err() != nil || errors.Is(cause, context.Canceled) || errors.Is(cause, context.DeadlineExceeded) {
		return cause
	}
	hasDisk, err := manager.storage.HasDisk(ctx, machine.ID)
	if err != nil {
		return errors.Join(cause, err)
	}
	if !hasDisk {
		return fmt.Errorf("cannot cold boot VM %s without its disk: %w", machine.ID, cause)
	}

	// A failed cold boot must not retry remote memory over a disk the guest may have changed.
	observed.SleepState = SleepStateAwake
	if err := manager.store.writeObserved(machine.ID, *observed); err != nil {
		return errors.Join(cause, err)
	}
	_, err = manager.recoverFailedRestore(ctx, resultState, machine, cause)
	return err
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
	if err := manager.applyDesiredSpecification(ctx, desired, machine, RuntimeStatus{State: StateStopped}, observed, operationID); err != nil {
		return err
	}
	return manager.writeAppliedState(desired, observed, StateStopped, SleepStateAwake)
}

// writeAppliedState records a complete state transition at the desired generations.
func (manager *Manager) writeAppliedState(desired DesiredRecord, observed *ObservedRecord, state State, sleepState SleepState) error {
	observed.State = state
	observed.SleepState = sleepState
	observed.Generation = desired.Generation
	observed.SpecificationGeneration = desired.SpecificationGeneration
	observed.RestartGeneration = desired.RestartGeneration
	observed.completeOperation()
	return manager.store.writeObserved(desired.ID, *observed)
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

// applyRestart restarts a VM with newer restart intent.
func (manager *Manager) applyRestart(
	ctx context.Context,
	identifier string,
	machine RuntimeMachine,
	status RuntimeStatus,
	observed *ObservedRecord,
	operationID string,
) (RuntimeStatus, error) {
	if status.State != StateRunning && status.State != StatePaused {
		return status, nil
	}
	err := manager.runOperation(ctx, identifier, observed, operationID, phaseRestart, func() error {
		if err := manager.runtime.Stop(ctx, machine); err != nil {
			return err
		}
		return manager.runtime.Start(ctx, machine)
	})
	if err != nil {
		return status, err
	}
	return RuntimeStatus{State: StateRunning}, nil
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
	err := manager.runOperation(ctx, identifier, observed, operationID, phase, func() error {
		return operation(ctx, machine)
	})
	return RuntimeStatus{State: resultState}, err
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
) (operationError error) {
	now := time.Now().UTC()
	defer func() { manager.recordOperation(phase, now, operationError) }()
	observed.Phase = phase
	if observed.OperationID != operationID {
		observed.OperationStartedAt = now
	}
	observed.OperationID = operationID
	observed.UpdatedAt = now
	// The last error stays visible until the operation completes, so a retry does not hide it.
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
	level := slog.LevelInfo
	if isRoutinePhase(phase) {
		level = slog.LevelDebug
	}
	manager.logger.Log(ctx, level, "virtual machine operation completed",
		"component", "vm", "operation", "reconcile", "phase", phase, "virtual_machine_id", identifier,
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
		"component", "vm", "operation", "reconcile", "phase", phase, "virtual_machine_id", identifier,
		"operation_id", observed.OperationID, "applied_generation", observed.Generation,
		"observed_state", observed.State, "duration", time.Since(startedAt), "error", err,
	)
}
