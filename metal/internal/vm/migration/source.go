package migration

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

// SourceDescription contains the VM definition and observed state from the source.
type SourceDescription struct {
	Definition    VirtualMachineDefinition `json:"config"`
	ObservedState vm.State                 `json:"observed_state"`
}

// SourceSnapshot is the source reply to a snapshot request.
type SourceSnapshot struct {
	Sequence  int    `json:"sequence"`
	SizeBytes int64  `json:"size_bytes"`
	GUID      string `json:"guid"`
}

// SnapshotAcknowledgement acknowledges the last snapshot that the destination received.
type SnapshotAcknowledgement struct {
	ReceivedSequence int `json:"received_sequence,omitempty"`
}

// SnapshotStreamRequest describes the snapshot stream that the source must start.
type SnapshotStreamRequest struct {
	Sequence        int    `json:"sequence"`
	ResumeToken     string `json:"resume_token,omitempty"`
	ThroughputMiBps int    `json:"throughput_mibps,omitempty"`
}

// migrationSnapshotName includes the migration ID to avoid stale collisions.
func migrationSnapshotName(migrationID string, sequence int) string {
	return fmt.Sprintf("migration-%s-%d", migrationID, sequence)
}

// sourceIdleTimeout follows Atlas's 10-minute destination visibility timeout.
const sourceIdleTimeout = 15 * time.Minute

func (m *Manager) writeSourceContact(record sourceRecord) error {
	record.LastContactAt = m.now()
	return m.store.writeSource(record)
}

func (m *Manager) hasSourceStream(virtualMachineID string) bool {
	m.sourceStreamsMutex.Lock()
	defer m.sourceStreamsMutex.Unlock()
	return m.sourceStreams[virtualMachineID] != nil
}

// isSourceExpirable reports whether the host can safely release the source lock.
// A stopped source can have a running destination, so it always needs an operator.
func (m *Manager) isSourceExpirable(record sourceRecord) bool {
	if record.State != sourceLocked {
		return false
	}
	if m.hasSourceStream(record.VirtualMachineID) {
		return false
	}
	return m.now().Sub(record.LastContactAt) > sourceIdleTimeout
}

// ExpiredSourceVirtualMachineIDs returns the VMs whose source lock can be released.
func (m *Manager) ExpiredSourceVirtualMachineIDs(_ context.Context) ([]string, error) {
	virtualMachineIDs, err := m.store.listVirtualMachineIDs()
	if err != nil {
		return nil, err
	}
	expirable := make([]string, 0, len(virtualMachineIDs))
	for _, virtualMachineID := range virtualMachineIDs {
		if !m.store.has(m.store.sourcePath(virtualMachineID)) {
			continue
		}
		record, err := m.store.readSource(virtualMachineID)
		if err != nil {
			return nil, err
		}
		if m.isSourceExpirable(record) {
			expirable = append(expirable, virtualMachineID)
		}
	}
	return expirable, nil
}

// ExpireSource releases an idle pre-stop source lock and leaves a tombstone.
func (m *Manager) ExpireSource(ctx context.Context, virtualMachineID string) error {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	// Re-read under the lock because StopSource uses the same lock.
	record, err := m.store.readSource(virtualMachineID)
	if errors.Is(err, vm.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if !m.isSourceExpirable(record) {
		return nil
	}

	if err := m.removeMigrationSnapshots(ctx, record); err != nil {
		return err
	}
	if record.TemporaryDiskLimitMiBps > 0 {
		if err := m.host.RefreshSourceDisk(ctx, virtualMachineID); err != nil {
			return err
		}
	}

	record.State = sourceExpired
	if err := m.store.writeSource(record); err != nil {
		return err
	}
	m.logger.Warn("released an idle migration source lock",
		"migration_id", record.ID, "virtual_machine_id", virtualMachineID,
		"idle_seconds", int(m.now().Sub(record.LastContactAt).Seconds()))
	return nil
}

// LockSource locks the source and returns the VM definition and observed state.
// Missing, failed, unknown, or already-migrating VMs are rejected.
func (m *Manager) LockSource(ctx context.Context, migrationID, virtualMachineID string) (SourceDescription, error) {
	if !vm.ValidIdentifier(migrationID) || !vm.ValidIdentifier(virtualMachineID) {
		return SourceDescription{}, vm.ErrConflict
	}
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return SourceDescription{}, err
	}
	defer unlock()

	if existing, err := m.store.readSource(virtualMachineID); err == nil {
		if existing.State != sourceExpired {
			if existing.ID != migrationID {
				return SourceDescription{}, vm.ErrConflict
			}
			if err := m.writeSourceContact(existing); err != nil {
				return SourceDescription{}, err
			}
			return m.describeSource(virtualMachineID)
		}
		// The expired migration cannot revive its lock. A new migration replaces it.
		if existing.ID == migrationID {
			return SourceDescription{}, vm.ErrConflict
		}
	} else if !errors.Is(err, vm.ErrNotFound) {
		return SourceDescription{}, err
	}

	desired, err := m.host.ReadDesired(virtualMachineID)
	if err != nil {
		return SourceDescription{}, err
	}
	observed, err := m.host.ReadObserved(virtualMachineID)
	if err != nil {
		return SourceDescription{}, err
	}
	if desired.Specification.Rescue.Enabled || observed.RescueGeneration != desired.RescueGeneration ||
		observed.State == vm.StateFailed || observed.State == vm.StateUnknown {
		return SourceDescription{}, vm.ErrConflict
	}

	record := sourceRecord{
		ID:               migrationID,
		VirtualMachineID: virtualMachineID,
		OriginalDesired:  desired.State,
		State:            sourceLocked,
	}
	if err := m.writeSourceContact(record); err != nil {
		return SourceDescription{}, err
	}
	return m.describeSource(virtualMachineID)
}

// UnlockSource removes snapshots, restores the disk limit, and unlocks the VM.
// It refuses a stopped source before rollback completes.
func (m *Manager) UnlockSource(ctx context.Context, migrationID, virtualMachineID string) error {
	if !vm.ValidIdentifier(virtualMachineID) {
		return vm.ErrConflict
	}
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	existing, err := m.store.readSource(virtualMachineID)
	if errors.Is(err, vm.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if existing.ID != migrationID {
		return vm.ErrConflict
	}
	if existing.State == sourceExpired {
		return vm.ErrConflict
	}
	if existing.State == sourceStopped || existing.State == sourceDetached {
		return vm.ErrConflict
	}
	if err := m.stopSourceStream(ctx, virtualMachineID); err != nil {
		return err
	}
	if err := m.removeMigrationSnapshots(ctx, existing); err != nil {
		return err
	}
	if existing.TemporaryDiskLimitMiBps > 0 && existing.State == sourceLocked {
		if err := m.host.RefreshSourceDisk(ctx, virtualMachineID); err != nil {
			return err
		}
	}
	return m.store.remove(virtualMachineID)
}

// StartSourceRollback restores a stopped source during abort.
func (m *Manager) StartSourceRollback(ctx context.Context, migrationID, virtualMachineID string) error {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	record, err := m.boundSourceRecord(virtualMachineID, migrationID)
	if err != nil {
		return err
	}
	if record.State == sourceRestored {
		return nil
	}
	if record.State != sourceStopped && record.State != sourceDetached {
		return vm.ErrConflict
	}
	if err := m.host.EnsureMigrationNetwork(ctx, virtualMachineID); err != nil {
		return err
	}
	if err := m.host.RestoreRuntimeState(ctx, virtualMachineID, record.OriginalDesired); err != nil {
		return err
	}
	record.State = sourceRestored
	return m.writeSourceContact(record)
}

// DestroySource removes a stopped source and its migration state. It requires a
// stopped, network-removed source with a final snapshot.
func (m *Manager) DestroySource(ctx context.Context, migrationID, virtualMachineID string) error {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	record, err := m.store.readSource(virtualMachineID)
	if errors.Is(err, vm.ErrNotFound) {
		return m.assertNoSourceRemnant(ctx, virtualMachineID)
	}
	if err != nil {
		return err
	}
	if record.ID != migrationID {
		return vm.ErrConflict
	}
	if record.State != sourceDetached && record.State != sourceRuntimeRemoved && record.State != sourceStorageRemoved {
		return vm.ErrConflict
	}
	if record.FinalSequence < 1 {
		return vm.ErrConflict
	}
	if err := m.stopSourceStream(ctx, virtualMachineID); err != nil {
		return err
	}

	if record.State == sourceDetached {
		if err := m.host.RemoveMigratedRuntime(ctx, virtualMachineID); err != nil {
			return err
		}
		record.State = sourceRuntimeRemoved
		if err := m.writeSourceContact(record); err != nil {
			return err
		}
	}
	if record.State == sourceRuntimeRemoved {
		if err := m.host.ReleaseStorage(ctx, virtualMachineID); err != nil {
			return err
		}
		record.State = sourceStorageRemoved
		if err := m.writeSourceContact(record); err != nil {
			return err
		}
	}
	return m.host.RemoveVirtualMachineRecords(virtualMachineID)
}

func (m *Manager) assertNoSourceRemnant(ctx context.Context, virtualMachineID string) error {
	if _, err := m.host.ReadDesired(virtualMachineID); err == nil {
		return vm.ErrConflict
	} else if !errors.Is(err, vm.ErrNotFound) {
		return err
	}
	exists, err := m.disks.DestinationDatasetExists(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	if exists {
		return vm.ErrConflict
	}
	return nil
}

func (m *Manager) removeMigrationSnapshots(ctx context.Context, record sourceRecord) error {
	highest := max(record.Sequence, record.FinalSequence)
	for sequence := 1; sequence <= highest; sequence++ {
		if err := m.disks.RemoveSnapshot(ctx, record.VirtualMachineID, migrationSnapshotName(record.ID, sequence)); err != nil {
			return err
		}
	}
	return nil
}

// NextSourceSnapshot records the acknowledged sequence and returns the next
// snapshot. A repeat returns the same unacknowledged snapshot.
func (m *Manager) NextSourceSnapshot(ctx context.Context, migrationID, virtualMachineID string, receivedSequence int) (SourceSnapshot, error) {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return SourceSnapshot{}, err
	}
	defer unlock()

	record, err := m.boundSourceRecord(virtualMachineID, migrationID)
	if err != nil {
		return SourceSnapshot{}, err
	}
	if record.State != sourceLocked {
		return SourceSnapshot{}, vm.ErrConflict
	}
	if receivedSequence < 0 || receivedSequence > record.Sequence {
		return SourceSnapshot{}, vm.ErrConflict
	}

	// A repeat of the same request changes nothing else, so stamp the contact
	// here rather than only on the paths that advance the sequence.
	if err := m.writeSourceContact(record); err != nil {
		return SourceSnapshot{}, err
	}

	if receivedSequence > record.AcknowledgedSequence {
		record.AcknowledgedSequence = receivedSequence
		if err := m.writeSourceContact(record); err != nil {
			return SourceSnapshot{}, err
		}
		if receivedSequence >= 2 {
			_ = m.disks.RemoveSnapshot(ctx, virtualMachineID, migrationSnapshotName(migrationID, receivedSequence-1))
		}
	}

	if record.Sequence <= record.AcknowledgedSequence {
		next := record.AcknowledgedSequence + 1
		if err := m.disks.CreateSnapshot(ctx, virtualMachineID, migrationSnapshotName(migrationID, next)); err != nil {
			return SourceSnapshot{}, err
		}
		record.Sequence = next
		if err := m.writeSourceContact(record); err != nil {
			return SourceSnapshot{}, err
		}
	}
	return m.describeSnapshot(ctx, virtualMachineID, migrationID, record.Sequence)
}

// StopSource records the last received sequence, stops the source, and creates
// the final snapshot. Checkpoints make repeats safe.
func (m *Manager) StopSource(ctx context.Context, migrationID, virtualMachineID string, receivedSequence int) (SourceSnapshot, error) {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return SourceSnapshot{}, err
	}
	defer unlock()

	record, err := m.boundSourceRecord(virtualMachineID, migrationID)
	if err != nil {
		return SourceSnapshot{}, err
	}
	if record.State != sourceLocked && record.State != sourceStopped && record.State != sourceDetached {
		return SourceSnapshot{}, vm.ErrConflict
	}
	if receivedSequence < 0 || receivedSequence > record.Sequence {
		return SourceSnapshot{}, vm.ErrConflict
	}

	if record.State == sourceLocked {
		if err := m.host.NormalizeSourceToStopped(ctx, virtualMachineID); err != nil {
			return SourceSnapshot{}, err
		}
		record.State = sourceStopped
		if err := m.writeSourceContact(record); err != nil {
			return SourceSnapshot{}, err
		}
	}
	if record.State == sourceStopped {
		if err := m.host.RemoveMigrationNetwork(ctx, virtualMachineID); err != nil {
			return SourceSnapshot{}, err
		}
		record.State = sourceDetached
		if err := m.writeSourceContact(record); err != nil {
			return SourceSnapshot{}, err
		}
	}
	if record.FinalSequence == 0 {
		final := max(record.AcknowledgedSequence, receivedSequence) + 1
		name := migrationSnapshotName(migrationID, final)
		// Replace any untransferred candidate with the post-stop snapshot.
		if err := m.disks.RemoveSnapshot(ctx, virtualMachineID, name); err != nil {
			return SourceSnapshot{}, err
		}
		if err := m.disks.CreateSnapshot(ctx, virtualMachineID, name); err != nil {
			return SourceSnapshot{}, err
		}
		record.Sequence = final
		record.FinalSequence = final
		if err := m.writeSourceContact(record); err != nil {
			return SourceSnapshot{}, err
		}
	}
	return m.describeSnapshot(ctx, virtualMachineID, migrationID, record.FinalSequence)
}

// StartSourceStream starts one mutual-TLS listener that sends the requested ZFS stream.
func (m *Manager) StartSourceStream(ctx context.Context, migrationID, virtualMachineID string, sequence int, resumeToken string, throughputMiBps int) error {
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return err
	}
	defer unlock()

	record, err := m.boundSourceRecord(virtualMachineID, migrationID)
	if err != nil {
		return err
	}
	if record.State != sourceLocked && record.State != sourceStopped && record.State != sourceDetached {
		return vm.ErrConflict
	}
	if err := m.writeSourceContact(record); err != nil {
		return err
	}
	if sequence != record.Sequence {
		return vm.ErrConflict
	}
	m.transfersMutex.Lock()
	if m.closed {
		m.transfersMutex.Unlock()
		return vm.ErrConflict
	}
	m.sourceStreamsWaitGroup.Add(1)
	m.transfersMutex.Unlock()
	waitRegistered := true
	defer func() {
		if waitRegistered {
			m.sourceStreamsWaitGroup.Done()
		}
	}()

	m.sourceStreamsMutex.Lock()
	if len(m.sourceStreams) > 0 {
		m.sourceStreamsMutex.Unlock()
		return vm.ErrConflict
	}

	// The stream outlives its request, so a destination that never connects cannot hold the port.
	streamContext, cancel := context.WithTimeout(m.rootContext, maxTransferDuration)
	handle := &backgroundOperation{cancel: cancel, done: make(chan struct{})}
	m.sourceStreams[virtualMachineID] = handle
	m.sourceStreamsMutex.Unlock()
	if err := m.applySourceDiskLimit(ctx, record, throughputMiBps); err != nil {
		m.endSourceStream(virtualMachineID, handle)
		return err
	}

	name := migrationSnapshotName(migrationID, sequence)
	base := ""
	if sequence > 1 {
		base = migrationSnapshotName(migrationID, sequence-1)
	}
	server, err := m.disks.StartSnapshotServer(streamContext, virtualMachineID, name, base, resumeToken)
	if err != nil {
		m.endSourceStream(virtualMachineID, handle)
		return err
	}
	waitRegistered = false
	go func() {
		defer m.sourceStreamsWaitGroup.Done()
		if err := server.Wait(); err != nil && streamContext.Err() == nil {
			m.logger.Error("source snapshot stream failed", "migration_id", migrationID, "error", err)
		}
		m.endSourceStream(virtualMachineID, handle)
	}()
	return nil
}

func (m *Manager) endSourceStream(virtualMachineID string, handle *backgroundOperation) {
	m.sourceStreamsMutex.Lock()
	if m.sourceStreams[virtualMachineID] == handle {
		delete(m.sourceStreams, virtualMachineID)
	}
	m.sourceStreamsMutex.Unlock()

	handle.cancel()
	close(handle.done)
}

// stopSourceStream cancels an in-flight source stream for a VM and waits for it
// to exit. A later snapshot destroy then cannot fail on a busy dataset.
func (m *Manager) stopSourceStream(ctx context.Context, virtualMachineID string) error {
	m.sourceStreamsMutex.Lock()
	handle := m.sourceStreams[virtualMachineID]
	m.sourceStreamsMutex.Unlock()
	if handle == nil {
		return nil
	}

	handle.cancel()
	select {
	case <-handle.done:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for source stream: %w", ctx.Err())
	}
}

func (m *Manager) applySourceDiskLimit(ctx context.Context, record sourceRecord, throughputMiBps int) error {
	if throughputMiBps <= 0 {
		return nil
	}
	applied, err := m.host.LimitSourceDisk(ctx, record.VirtualMachineID, throughputMiBps)
	if err != nil {
		return err
	}
	if applied == 0 || applied == record.TemporaryDiskLimitMiBps {
		return nil
	}
	record.TemporaryDiskLimitMiBps = applied
	return m.writeSourceContact(record)
}

func (m *Manager) describeSnapshot(ctx context.Context, virtualMachineID, migrationID string, sequence int) (SourceSnapshot, error) {
	name := migrationSnapshotName(migrationID, sequence)
	base := ""
	if sequence > 1 {
		base = migrationSnapshotName(migrationID, sequence-1)
	}
	sizeBytes, err := m.disks.EstimateStreamBytes(ctx, virtualMachineID, name, base)
	if err != nil {
		return SourceSnapshot{}, err
	}
	guid, err := m.disks.SnapshotGUID(ctx, virtualMachineID, name)
	if err != nil {
		return SourceSnapshot{}, err
	}
	return SourceSnapshot{Sequence: sequence, SizeBytes: sizeBytes, GUID: guid}, nil
}

func (m *Manager) boundSourceRecord(virtualMachineID, migrationID string) (sourceRecord, error) {
	record, err := m.store.readSource(virtualMachineID)
	if err != nil {
		return sourceRecord{}, err
	}
	if record.ID != migrationID || record.State == sourceExpired {
		return sourceRecord{}, vm.ErrConflict
	}
	return record, nil
}

func (m *Manager) describeSource(virtualMachineID string) (SourceDescription, error) {
	desired, err := m.host.ReadDesired(virtualMachineID)
	if err != nil {
		return SourceDescription{}, err
	}
	observed, err := m.host.ReadObserved(virtualMachineID)
	if err != nil {
		return SourceDescription{}, err
	}
	return SourceDescription{
		Definition: VirtualMachineDefinition{
			VirtualMachineID:        desired.ID,
			CreateFingerprint:       desired.CreateFingerprint,
			Generation:              desired.Generation,
			SpecificationGeneration: desired.SpecificationGeneration,
			RestartGeneration:       desired.RestartGeneration,
			DesiredState:            desired.State,
			Specification:           vm.CloneSpecification(desired.Specification),
		},
		ObservedState: observed.State,
	}, nil
}
