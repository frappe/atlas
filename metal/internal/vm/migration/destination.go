package migration

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/frappe/atlas/metal/internal/vm"
)

// AdvanceDestination prepares the source and starts or resumes transfer. It returns
// without running transfer itself.
func (m *Manager) AdvanceDestination(ctx context.Context, virtualMachineID string) error {
	unlock, err := m.locks.Lock(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	record, err := m.store.readDestination(virtualMachineID)
	if errors.Is(err, vm.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if record.State == destinationRemovingRuntime || record.State == destinationRemovingStorage ||
		record.State == destinationRestoringSource || record.State == destinationUnlockingSource {
		m.StartTransfer(virtualMachineID)
		return nil
	}
	if record.State == destinationReady || record.State == destinationFinishing {
		// Atlas alone releases a ready destination because it may own the VM.
		if record.State == destinationFinishing {
			m.StartTransfer(virtualMachineID)
		}
		return nil
	}
	if m.isDestinationAbandoned(record) {
		m.logger.Warn("rolling back a migration destination whose controller stopped calling",
			"migration_id", record.ID, "virtual_machine_id", virtualMachineID,
			"status", record.State.status(), "phase", record.State.phase(),
			"idle_seconds", int(m.now().Sub(record.LastControlAt).Seconds()))
		record.State = destinationRemovingRuntime
		if err := m.store.writeDestination(record); err != nil {
			return err
		}
		m.StartTransfer(virtualMachineID)
		return nil
	}
	if record.State == destinationFailed || record.State.terminal() {
		return nil
	}
	if record.State == destinationCopying || record.State == destinationStopping ||
		record.State == destinationCreatingNetwork || record.State == destinationApplyingState {
		m.StartTransfer(virtualMachineID)
		return nil
	}
	// Expire destinations that never advanced past preparing.
	if m.now().Sub(record.CreatedAt) > reservationTimeout {
		m.logger.Warn("migration destination expired before source preparation started", "migration_id", record.ID)
		record.State = destinationRemovingRuntime
		if err := m.store.writeDestination(record); err != nil {
			return err
		}
		m.StartTransfer(virtualMachineID)
		return nil
	}

	definition, observedState, err := m.source.PrepareSource(ctx, record.Source, record.ID, virtualMachineID)
	if err != nil {
		return m.recordDestinationError(record, err)
	}
	if definition.VirtualMachineID != virtualMachineID {
		return m.recordDestinationError(record, fmt.Errorf("source returned definition for %s", definition.VirtualMachineID))
	}
	definition, err = record.Resize.apply(definition)
	if err != nil {
		return m.recordDestinationError(record, err)
	}
	if err := m.reserveAndReconstructDestination(ctx, record, definition, observedState); err != nil {
		return err
	}
	m.StartTransfer(virtualMachineID)
	return nil
}

// reserveAndReconstructDestination makes the capacity check and its reservation one
// host allocation operation.
func (m *Manager) reserveAndReconstructDestination(
	ctx context.Context,
	record destinationRecord,
	definition VirtualMachineDefinition,
	observedState vm.State,
) error {
	unlockAllocation := m.host.LockUserIDAllocation()
	defer unlockAllocation()

	if err := m.reserveShape(ctx, definition.Specification); err != nil {
		return m.recordDestinationError(record, err)
	}
	return m.reconstructDestination(record, definition, observedState)
}

// ActiveDestinationVirtualMachineIDs returns VM IDs for nonterminal destination migrations.
func (m *Manager) ActiveDestinationVirtualMachineIDs(_ context.Context) ([]string, error) {
	virtualMachineIDs, err := m.store.listVirtualMachineIDs()
	if err != nil {
		return nil, err
	}
	active := make([]string, 0, len(virtualMachineIDs))
	for _, virtualMachineID := range virtualMachineIDs {
		if !m.store.has(m.store.destinationPath(virtualMachineID)) {
			continue
		}
		record, err := m.store.readDestination(virtualMachineID)
		if err != nil {
			return nil, err
		}
		if !record.State.terminal() {
			active = append(active, virtualMachineID)
		}
	}
	return active, nil
}

// isDestinationAbandoned reports whether Atlas stopped driving this migration.
// Its caller excludes ready destinations because Atlas can already own their VM.
func (m *Manager) isDestinationAbandoned(record destinationRecord) bool {
	if record.State.terminal() || record.State == destinationFinishing ||
		record.State == destinationRemovingRuntime || record.State == destinationRemovingStorage ||
		record.State == destinationRestoringSource || record.State == destinationUnlockingSource {
		return false
	}
	return m.now().Sub(record.LastControlAt) > destinationIdleTimeout
}

func (m *Manager) reserveShape(ctx context.Context, specification vm.Specification) error {
	available, err := m.capacity(ctx)
	if err != nil {
		return err
	}
	if specification.MemoryMiB > available.MemoryMiB ||
		specification.DiskMiB > available.StorageMiB {
		return vm.ErrConflict
	}
	return nil
}

func (m *Manager) reconstructDestination(record destinationRecord, definition VirtualMachineDefinition, observedState vm.State) error {
	userID, err := m.reuseOrAllocateUserID(definition.VirtualMachineID)
	if err != nil {
		return m.recordDestinationError(record, err)
	}
	desired := vm.DesiredRecord{
		ID:                      definition.VirtualMachineID,
		UserID:                  userID,
		GroupID:                 userID,
		CreateFingerprint:       definition.CreateFingerprint,
		Generation:              definition.Generation,
		SpecificationGeneration: definition.SpecificationGeneration,
		RestartGeneration:       definition.RestartGeneration,
		State:                   definition.DesiredState,
		Specification:           vm.CloneSpecification(definition.Specification),
	}
	observed := vm.ObservedRecord{State: vm.StateUnknown, UpdatedAt: m.now()}
	if err := m.host.WriteDesired(desired); err != nil {
		return m.recordDestinationError(record, err)
	}
	if err := m.host.WriteObserved(definition.VirtualMachineID, observed); err != nil {
		return errors.Join(m.recordDestinationError(record, err), os.Remove(m.host.DesiredPath(definition.VirtualMachineID)))
	}

	record.Definition = &definition
	record.State = destinationCopying
	record.SourceObservedState = observedState
	record.CopyStartedAt = m.now()
	record.Error = nil
	return m.store.writeDestination(record)
}

func (m *Manager) reuseOrAllocateUserID(virtualMachineID string) (uint32, error) {
	if existing, err := m.host.ReadDesired(virtualMachineID); err == nil {
		return existing.UserID, nil
	} else if !errors.Is(err, vm.ErrNotFound) {
		return 0, err
	}
	return m.host.AllocateUserID()
}

func (m *Manager) recordDestinationError(record destinationRecord, cause error) error {
	record.Error = &vm.OperationError{Code: "migration_error", Message: cause.Error(), UpdatedAt: m.now()}
	if writeError := m.store.writeDestination(record); writeError != nil {
		return errors.Join(cause, writeError)
	}
	return cause
}

// RequestFinish records finish intent for a ready migration. A pending abort wins.
func (m *Manager) RequestFinish(ctx context.Context, migrationID string) error {
	virtualMachineID, _, err := m.store.findDestination(migrationID)
	if err != nil {
		return err
	}
	unlock, err := m.locks.Lock(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	record, err := m.store.readDestination(virtualMachineID)
	if err != nil {
		return err
	}
	if record.State == destinationCompleted {
		return nil
	}
	if record.State == destinationFinishing {
		return nil
	}
	if record.State != destinationReady {
		return vm.ErrConflict
	}
	record.State = destinationFinishing
	return m.store.writeDestination(record)
}

// advanceFinish destroys the source and writes a completed record. Each step is
// idempotent, so a retry after the source is gone still completes.
func (m *Manager) advanceFinish(ctx context.Context, record destinationRecord) error {
	if err := m.source.FinishSource(ctx, record.Source, record.ID, record.VirtualMachineID); err != nil {
		return err
	}
	if err := m.removeReceivedSnapshots(ctx, record); err != nil {
		return err
	}

	_, err := m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		*destination = terminalDestinationRecord(*destination, destinationCompleted, m.now())
	})
	return err
}

// removeReceivedSnapshots destroys the migration snapshots left on the received
// dataset after a successful migration. The live volume keeps its data. Repeats
// are safe.
func (m *Manager) removeReceivedSnapshots(ctx context.Context, record destinationRecord) error {
	for _, interval := range record.Intervals {
		name := migrationSnapshotName(record.ID, interval.Sequence)
		if err := m.disks.RemoveSnapshot(ctx, record.VirtualMachineID, name); err != nil {
			return err
		}
	}
	return nil
}

// advanceAbort cleans the destination, then unlocks or restores the source. Errors
// keep both hosts locked for the next pass.
func (m *Manager) advanceAbort(ctx context.Context, record destinationRecord) error {
	record, err := m.cleanAbortDestination(ctx, record)
	if err != nil {
		return err
	}

	// Restore a stopped source before unlocking it.
	if record.State == destinationRestoringSource {
		if err := m.source.StartSource(ctx, record.Source, record.ID, record.VirtualMachineID); err != nil {
			return err
		}
		if record, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
			destination.State = destinationUnlockingSource
		}); err != nil {
			return err
		}
	}
	if record.State == destinationUnlockingSource {
		if err := m.source.RemoveSource(ctx, record.Source, record.ID, record.VirtualMachineID); err != nil {
			return err
		}
	}

	_, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		*destination = terminalDestinationRecord(*destination, destinationAborted, m.now())
	})
	return err
}

func (m *Manager) cleanAbortDestination(ctx context.Context, record destinationRecord) (destinationRecord, error) {
	if record.State == destinationRemovingRuntime {
		if err := m.host.RemoveMigratedRuntime(ctx, record.VirtualMachineID); err != nil && !errors.Is(err, vm.ErrNotFound) {
			return record, err
		}
		updated, err := m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
			destination.State = destinationRemovingStorage
		})
		if err != nil {
			return record, err
		}
		record = updated
	}
	if record.State == destinationRemovingStorage {
		if err := m.disks.AbortReceive(ctx, record.VirtualMachineID); err != nil {
			return record, err
		}
		if err := m.removeDestinationStaging(record.VirtualMachineID); err != nil {
			return record, err
		}
		updated, err := m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
			if destination.SourceStopped {
				destination.State = destinationRestoringSource
			} else {
				destination.State = destinationUnlockingSource
			}
		})
		if err != nil {
			return record, err
		}
		record = updated
	}
	return record, nil
}
