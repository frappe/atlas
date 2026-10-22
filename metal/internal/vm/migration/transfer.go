package migration

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	// maxTransferIntervals bounds incremental rounds before cutover.
	maxTransferIntervals = 16
	// maxTransferDuration bounds copy time before cutover.
	maxTransferDuration = 30 * time.Minute
)

// throttleSteps are source disk limits in MiB/s for large deltas.
var throttleSteps = []int{64, 32, 16, 8}

// errTransferFailed marks a definite failure that already updated the record.
var errTransferFailed = errors.New("migration transfer failed")

// ErrNoMemoryToSave reports a memory snapshot request for a source that is not running or paused.
var ErrNoMemoryToSave = errors.New("source has no guest memory to save")

// backgroundOperation cancels one VM's background worker and signals its exit.
type backgroundOperation struct {
	cancel context.CancelFunc
	done   chan struct{}
}

// StartTransfer starts one worker per VM. A running worker is unchanged.
func (m *Manager) StartTransfer(virtualMachineID string) {
	m.transfersMutex.Lock()
	defer m.transfersMutex.Unlock()
	if m.closed {
		return
	}
	if _, active := m.transfers[virtualMachineID]; active {
		return
	}
	workerContext, cancel := context.WithCancel(m.rootContext)
	handle := &backgroundOperation{cancel: cancel, done: make(chan struct{})}
	m.transfers[virtualMachineID] = handle
	m.transfersWaitGroup.Add(1)
	go func() {
		defer m.transfersWaitGroup.Done()
		defer close(handle.done)
		defer m.forgetTransfer(virtualMachineID)
		m.runTransfer(workerContext, virtualMachineID)
	}()
}

// CancelTransfer stops a VM worker and waits for active streams to exit.
func (m *Manager) CancelTransfer(ctx context.Context, virtualMachineID string) error {
	m.transfersMutex.Lock()
	handle := m.transfers[virtualMachineID]
	m.transfersMutex.Unlock()
	if handle == nil {
		return nil
	}
	handle.cancel()
	select {
	case <-handle.done:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for migration worker: %w", ctx.Err())
	}
}

// Shutdown stops accepting workers and waits for the active ones to stop.
func (m *Manager) Shutdown(shutdownContext context.Context) error {
	m.transfersMutex.Lock()
	if !m.closed {
		m.closed = true
		m.rootCancel()
	}
	m.transfersMutex.Unlock()

	finished := make(chan struct{})
	go func() {
		m.transfersWaitGroup.Wait()
		m.sourceStreamsWaitGroup.Wait()
		close(finished)
	}()
	select {
	case <-finished:
		return nil
	case <-shutdownContext.Done():
		return fmt.Errorf("wait for migration transfers: %w", shutdownContext.Err())
	}
}

func (m *Manager) forgetTransfer(virtualMachineID string) {
	m.transfersMutex.Lock()
	delete(m.transfers, virtualMachineID)
	m.transfersMutex.Unlock()
}

// runTransfer advances one migration from copying to ready. Transient errors
// leave it for the next pass.
func (m *Manager) runTransfer(ctx context.Context, virtualMachineID string) {
	for {
		record, err := m.store.readDestination(virtualMachineID)
		if err != nil {
			return
		}

		var advanceError error
		switch record.State {
		case destinationCompleted, destinationAborted, destinationFailed, destinationReady:
			return
		case destinationRemovingRuntime, destinationRemovingStorage, destinationRestoringSource, destinationUnlockingSource:
			advanceError = m.advanceAbort(ctx, record)
		case destinationFinishing:
			advanceError = m.advanceFinish(ctx, record)
		case destinationCopying:
			advanceError = m.advanceCopying(ctx, record)
		case destinationStopping:
			advanceError = m.advanceStopping(ctx, record)
		case destinationCreatingNetwork, destinationApplyingState:
			advanceError = m.advanceStarting(ctx, record)
		default:
			return
		}
		if advanceError != nil {
			if ctx.Err() == nil && !errors.Is(advanceError, errTransferFailed) {
				m.logger.Error("migration transfer interrupted", "migration_id", record.ID, "error", advanceError)
			}
			return
		}
	}
}

// advanceCopying copies an interval or enters stopping. Running sources send a
// full first interval; other sources cut over first.
func (m *Manager) advanceCopying(ctx context.Context, record destinationRecord) error {
	if err := m.receiveKernel(ctx, record); err != nil {
		return err
	}

	last := lastCompletedSequence(record)
	if last == 0 && record.SourceObservedState != vm.StateRunning {
		return m.enterStopping(ctx, record)
	}
	if last >= 1 && m.transferLimitReached(record) {
		return m.enterStopping(ctx, record)
	}

	snapshot, err := m.source.NextSnapshot(ctx, record.Source, record.ID, record.VirtualMachineID, last)
	if err != nil {
		return err
	}
	if snapshot.Sequence < 1 {
		return fmt.Errorf("source returned an invalid sequence %d", snapshot.Sequence)
	}
	if last >= 1 && m.isSmallDelta(snapshot) {
		return m.enterStopping(ctx, record)
	}
	return m.receiveSnapshot(ctx, record, snapshot, m.intervalThroughput(record))
}

// advanceStopping pulls and verifies the final snapshot, then enters starting.
func (m *Manager) advanceStopping(ctx context.Context, record destinationRecord) error {
	final, err := m.source.StopSource(
		ctx,
		record.Source,
		record.ID,
		record.VirtualMachineID,
		lastCompletedSequence(record),
		record.HasMemorySnapshot,
	)
	if errors.Is(err, ErrNoMemoryToSave) {
		return m.failTransfer(ctx, record, "stop source: "+err.Error())
	}
	if err != nil {
		return err
	}
	if final.Sequence < 1 || (record.HasMemorySnapshot && final.GUID == "") {
		return m.failTransfer(ctx, record, "source returned an invalid final sequence")
	}

	record, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		destination.SourceObservedState = vm.StateStopped
		destination.SourceStopped = true
	})
	if err != nil {
		return err
	}
	if record.State != destinationStopping {
		return nil
	}
	if err := m.receiveFinalSnapshots(ctx, record, final); err != nil {
		return err
	}

	_, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		if destination.State == destinationStopping {
			destination.State = destinationCreatingNetwork
		}
	})
	return err
}

// advanceStarting creates the destination network, applies VM state, and marks ready.
// A failure keeps both hosts locked for rollback.
func (m *Manager) advanceStarting(ctx context.Context, record destinationRecord) error {
	unlock, err := m.host.LockOperation(ctx, record.VirtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	if record.State == destinationCreatingNetwork {
		if err := m.host.EnsureMigrationNetwork(ctx, record.VirtualMachineID); err != nil {
			if isWorkerCancelled(ctx, err) {
				return err
			}
			return m.failTransfer(ctx, record, "create destination network: "+err.Error())
		}
		if record, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
			if destination.State == destinationCreatingNetwork {
				destination.State = destinationApplyingState
			}
		}); err != nil {
			return err
		}
	}
	if record.State == destinationApplyingState {
		if err := m.applyDestinationState(ctx, record); err != nil {
			return err
		}
	}

	_, err = m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		if destination.State == destinationApplyingState {
			destination.State = destinationReady
		}
	})
	return err
}

func (m *Manager) enterStopping(ctx context.Context, record destinationRecord) error {
	_, err := m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		if destination.State == destinationCopying {
			destination.State = destinationStopping
		}
	})
	return err
}

func (m *Manager) isSmallDelta(snapshot SourceSnapshot) bool {
	return snapshot.SizeBytes <= int64(m.finalDeltaMiB)*1024*1024
}

// intervalThroughput returns the next source disk limit. The first interval is
// unlimited; each later delta lowers the limit one step.
func (m *Manager) intervalThroughput(record destinationRecord) int {
	if lastCompletedSequence(record) == 0 {
		return 0
	}
	throttled := 0
	for _, interval := range record.Intervals {
		if interval.Completed && interval.ThroughputMiBps > 0 {
			throttled++
		}
	}
	if throttled >= len(throttleSteps) {
		return throttleSteps[len(throttleSteps)-1]
	}
	return throttleSteps[throttled]
}

func (m *Manager) receiveSnapshot(ctx context.Context, record destinationRecord, snapshot SourceSnapshot, throughputMiBps int) error {
	for _, interval := range record.Intervals {
		if interval.Sequence == snapshot.Sequence && interval.Completed {
			if interval.GUID != snapshot.GUID {
				return m.failTransfer(ctx, record, "completed snapshot GUID does not match the source")
			}
			return nil
		}
	}
	resumeToken, err := m.disks.ReceiveResumeToken(ctx, record.VirtualMachineID)
	if err != nil {
		return err
	}
	if resumeToken == "" && snapshot.Sequence == 1 {
		exists, err := m.disks.DestinationDatasetExists(ctx, record.VirtualMachineID)
		if err != nil {
			return err
		}
		if exists {
			return m.failTransfer(ctx, record, "unrelated destination dataset already exists")
		}
	}

	record, err = m.beginInterval(ctx, record, snapshot, throughputMiBps)
	if err != nil {
		return err
	}
	if record.State != destinationCopying && record.State != destinationStopping {
		return nil
	}
	estimatedBytes, err := m.streamInterval(ctx, record, snapshot, resumeToken, throughputMiBps)
	if err != nil {
		return err
	}

	receivedGUID, err := m.disks.SnapshotGUID(ctx, record.VirtualMachineID, migrationSnapshotName(record.ID, snapshot.Sequence))
	if err != nil {
		return err
	}
	if receivedGUID != snapshot.GUID {
		return m.failTransfer(ctx, record, "received snapshot GUID does not match the source")
	}

	return m.completeInterval(ctx, record, snapshot.Sequence, estimatedBytes, receivedGUID)
}

func (m *Manager) streamInterval(ctx context.Context, record destinationRecord, snapshot SourceSnapshot, resumeToken string, throughputMiBps int) (int64, error) {
	if err := m.source.StartSnapshotStream(
		ctx, record.Source, record.ID, record.VirtualMachineID,
		snapshot.Sequence, resumeToken, throughputMiBps,
	); err != nil {
		return 0, err
	}
	if err := m.disks.ReceiveSnapshotTLS(ctx, record.VirtualMachineID, record.Source); err != nil {
		return 0, err
	}
	return snapshot.SizeBytes, nil
}

func (m *Manager) beginInterval(ctx context.Context, record destinationRecord, snapshot SourceSnapshot, throughputMiBps int) (destinationRecord, error) {
	return m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		if hasInterval(destination.Intervals, snapshot.Sequence) {
			return
		}
		destination.Intervals = append(destination.Intervals, TransferProgress{
			Sequence:        snapshot.Sequence,
			StartedAt:       m.now(),
			TotalBytes:      snapshot.SizeBytes,
			ThroughputMiBps: throughputMiBps,
		})
	})
}

func (m *Manager) completeInterval(ctx context.Context, record destinationRecord, sequence int, bytesReceived int64, guid string) error {
	// The snapshot is already on the dataset. An unrecorded interval makes the
	// next pass ask for it again, which zfs receive then rejects.
	finishedAt := m.now()
	_, err := m.mutateDestination(context.WithoutCancel(ctx), record.VirtualMachineID, func(destination *destinationRecord) {
		for index := range destination.Intervals {
			if destination.Intervals[index].Sequence != sequence {
				continue
			}
			interval := &destination.Intervals[index]
			interval.BytesTransferred = bytesReceived
			interval.GUID = guid
			interval.Completed = true
			if interval.FinishedAt.IsZero() {
				interval.FinishedAt = finishedAt
			}
			interval.DurationSeconds = int(interval.FinishedAt.Sub(interval.StartedAt).Seconds())
		}
	})
	return err
}

// isWorkerCancelled identifies a shutdown that leaves checkpointed work safe to retry.
func isWorkerCancelled(ctx context.Context, err error) bool {
	return ctx.Err() != nil && (errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded))
}

// failTransfer marks the migration failed and keeps the dataset and snapshots.
func (m *Manager) failTransfer(ctx context.Context, record destinationRecord, message string) error {
	// A cancelled worker must still leave the reason on the record.
	if _, err := m.mutateDestination(context.WithoutCancel(ctx), record.VirtualMachineID, func(destination *destinationRecord) {
		if destination.State != destinationCopying && destination.State != destinationStopping &&
			destination.State != destinationCreatingNetwork && destination.State != destinationApplyingState {
			return
		}
		destination.State = destinationFailed
		destination.Error = &vm.OperationError{Code: "transfer_failed", Message: message, UpdatedAt: m.now()}
	}); err != nil {
		return err
	}
	return errTransferFailed
}

// transferLimitReached reports whether the copy hit the interval or time limit.
func (m *Manager) transferLimitReached(record destinationRecord) bool {
	completed := 0
	for _, interval := range record.Intervals {
		if interval.Completed {
			completed++
		}
	}
	if completed >= maxTransferIntervals {
		return true
	}
	return !record.CopyStartedAt.IsZero() && m.now().Sub(record.CopyStartedAt) > maxTransferDuration
}

func lastCompletedSequence(record destinationRecord) int {
	last := 0
	for _, interval := range record.Intervals {
		if interval.Completed && interval.Sequence > last {
			last = interval.Sequence
		}
	}
	return last
}

func hasInterval(intervals []TransferProgress, sequence int) bool {
	for _, interval := range intervals {
		if interval.Sequence == sequence {
			return true
		}
	}
	return false
}

// applyDestinationState restores the memory snapshot paused, or cold-boots the received disk for
// a disk-only migration or an allowed fallback.
func (m *Manager) applyDestinationState(ctx context.Context, record destinationRecord) error {
	if record.usesMemorySnapshot() {
		err := m.restoreMemorySnapshot(ctx, record)
		if err == nil || !record.AllowDiskOnlyFallback || isWorkerCancelled(ctx, err) {
			return m.failApply(ctx, record, err)
		}
		// The cleanup is repeatable, so a failure retries on the next pass.
		if record, err = m.discardMemorySnapshot(ctx, record, err); err != nil {
			return err
		}
	}
	return m.failApply(ctx, record, m.host.ApplyMigratedDestinationState(ctx, record.VirtualMachineID))
}

// failApply fails the transfer for an apply error. A cancelled worker leaves it for the next pass.
func (m *Manager) failApply(ctx context.Context, record destinationRecord, err error) error {
	if err == nil || isWorkerCancelled(ctx, err) {
		return err
	}
	return m.failTransfer(ctx, record, "apply destination state: "+err.Error())
}

// discardMemorySnapshot removes a failed memory restore and its network, recreates the
// network, and records the cause, so the received disk cold-boots like a disk-only migration.
func (m *Manager) discardMemorySnapshot(ctx context.Context, record destinationRecord, cause error) (destinationRecord, error) {
	m.logger.Warn("memory snapshot restore failed, cold booting the received disk",
		"migration_id", record.ID, "virtual_machine_id", record.VirtualMachineID, "error", cause)
	if err := m.host.RemoveMigratedRuntime(ctx, record.VirtualMachineID); err != nil {
		return record, errors.Join(cause, err)
	}
	if err := m.host.EnsureMigrationNetwork(ctx, record.VirtualMachineID); err != nil {
		return record, errors.Join(cause, err)
	}
	return m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		destination.MemorySnapshotError = cause.Error()
	})
}
