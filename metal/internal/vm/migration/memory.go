package migration

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"

	"github.com/klauspost/compress/zstd"

	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// OpenSourceMemorySnapshotFile opens immutable state only after source cutover completes.
func (m *Manager) OpenSourceMemorySnapshotFile(ctx context.Context, migrationID, virtualMachineID string, sequence int, name string) (*os.File, error) {
	if !vm.ValidIdentifier(virtualMachineID) || (name != "state" && name != "memory" && name != "metadata.json") {
		return nil, vm.ErrConflict
	}
	unlock, err := m.host.LockOperation(ctx, virtualMachineID)
	if err != nil {
		return nil, err
	}
	defer unlock()

	record, err := m.boundSourceRecord(virtualMachineID, migrationID)
	if err != nil {
		return nil, err
	}
	if record.Sleep != nil || record.State != sourceDetached || record.HasMemorySnapshot == nil || !*record.HasMemorySnapshot || sequence < 1 || sequence != record.FinalSequence {
		return nil, vm.ErrConflict
	}
	path := filepath.Join(m.host.VirtualMachineRecordsDirectory(), virtualMachineID, "saved-state", name)
	information, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !information.Mode().IsRegular() || information.Size() <= 0 {
		return nil, fmt.Errorf("invalid memory snapshot file %s", name)
	}
	// The open descriptor remains valid if rollback or finish unlinks this file.
	return os.Open(path)
}

// ReceiveMemorySnapshot downloads the memory snapshot into migration staging.
func (c *SourceClient) ReceiveMemorySnapshot(ctx context.Context, address, migrationID, virtualMachineID string, sequence int, directory string, memoryMiB int) error {
	for _, name := range []string{"state", "memory", "metadata.json"} {
		limit := int64(64 << 20)
		switch name {
		case "memory":
			limit = int64(memoryMiB) << 20
		case "metadata.json":
			limit = 1 << 20
		}
		if err := c.receiveMemorySnapshotFile(ctx, address, migrationID, virtualMachineID, sequence, directory, name, limit); err != nil {
			return fmt.Errorf("receive memory snapshot %s: %w", name, err)
		}
	}
	return nil
}

func (c *SourceClient) receiveMemorySnapshotFile(ctx context.Context, address, migrationID, virtualMachineID string, sequence int, directory, name string, limit int64) error {
	path := sourcePath(migrationID, virtualMachineID, "/memory-snapshot") + "&" + url.Values{
		"sequence": {strconv.Itoa(sequence)}, "file": {name},
	}.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address+path, nil)
	if err != nil {
		return err
	}
	response, err := c.transferClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("source returned HTTP %d", response.StatusCode)
	}
	decoder, err := zstd.NewReader(response.Body)
	if err != nil {
		return err
	}
	defer decoder.Close()

	file, err := os.OpenFile(filepath.Join(directory, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	// The zstd frame checksum and content size reject a corrupt or truncated stream.
	size, copyError := io.CopyBuffer(&storage.SparseWriter{File: file}, io.LimitReader(decoder, limit+1), make([]byte, 1<<20))
	if err := errors.Join(copyError, file.Truncate(size), file.Close()); err != nil {
		return err
	}
	if size <= 0 || size > limit {
		return fmt.Errorf("invalid file size %d (limit %d)", size, limit)
	}
	return nil
}

func (m *Manager) receiveFinalSnapshots(ctx context.Context, record destinationRecord, final SourceSnapshot) error {
	if !record.HasMemorySnapshot || record.hasSleepCheckpoint() {
		return m.receiveSnapshot(ctx, record, final, 0)
	}
	// Reserve the final disk row before the parallel memory row becomes visible.
	record, err := m.beginInterval(ctx, record, final, 0)
	if err != nil {
		return err
	}
	transferContext, cancel := context.WithCancel(ctx)
	defer cancel()
	memoryResult := make(chan error, 1)
	go func() {
		err := m.receiveMemorySnapshot(transferContext, record, final)
		if err != nil {
			cancel()
		}
		memoryResult <- err
	}()
	diskError := m.receiveSnapshot(transferContext, record, final, 0)
	if diskError != nil {
		cancel()
	}
	return errors.Join(diskError, <-memoryResult)
}

func (m *Manager) receiveMemorySnapshot(ctx context.Context, record destinationRecord, final SourceSnapshot) error {
	if record.MemorySnapshotDiskGUID != "" {
		if record.MemorySnapshotDiskGUID != final.GUID {
			return m.failTransfer(ctx, record, "memory snapshot belongs to a different disk snapshot")
		}
		return nil
	}
	desired, err := m.host.ReadDesired(record.VirtualMachineID)
	if err != nil {
		return err
	}
	pending, err := m.store.prepareMemorySnapshot(record.VirtualMachineID)
	if err != nil {
		return err
	}
	defer os.RemoveAll(pending)

	if _, err := m.mutateDestination(ctx, record.VirtualMachineID, func(destination *destinationRecord) {
		destination.MemoryTransfer = &TransferProgress{
			Sequence: final.Sequence + 1, StartedAt: m.now(), TotalBytes: int64(desired.Specification.MemoryMiB) << 20,
		}
	}); err != nil {
		return err
	}
	if err := m.source.ReceiveMemorySnapshot(ctx, record.Source, record.ID, record.VirtualMachineID, final.Sequence, pending, desired.Specification.MemoryMiB); err != nil {
		return err
	}
	totalBytes, err := m.store.publishMemorySnapshot(record.VirtualMachineID, pending, final)
	if err != nil {
		return err
	}
	return m.completeMemoryTransfer(ctx, record.VirtualMachineID, final.GUID, totalBytes)
}

func (store *migrationStore) prepareMemorySnapshot(virtualMachineID string) (string, error) {
	pending := store.memorySnapshotDirectory(virtualMachineID) + "-pending"
	// ponytail: incomplete memory snapshots restart; add byte ranges if retry cost matters.
	if err := os.RemoveAll(pending); err != nil {
		return "", err
	}
	if err := os.Mkdir(pending, 0o700); err != nil {
		return "", err
	}
	return pending, nil
}

func (store *migrationStore) publishMemorySnapshot(virtualMachineID, pending string, final SourceSnapshot) (int64, error) {
	var totalBytes int64
	for _, name := range []string{"state", "memory", "metadata.json"} {
		information, err := os.Stat(filepath.Join(pending, name))
		if err != nil {
			return 0, err
		}
		totalBytes += information.Size()
	}
	if err := writeMigrationRecord(filepath.Join(pending, "disk-snapshot.json"), final); err != nil {
		return 0, err
	}

	published := store.memorySnapshotDirectory(virtualMachineID)
	if err := os.RemoveAll(published); err != nil {
		return 0, err
	}
	if err := os.Rename(pending, published); err != nil {
		return 0, err
	}
	return totalBytes, nil
}

func (m *Manager) completeMemoryTransfer(ctx context.Context, virtualMachineID, guid string, totalBytes int64) error {
	_, err := m.mutateDestination(context.WithoutCancel(ctx), virtualMachineID, func(destination *destinationRecord) {
		destination.MemorySnapshotDiskGUID = guid
		transfer := destination.MemoryTransfer
		transfer.FinishedAt = m.now()
		transfer.DurationSeconds = int(transfer.FinishedAt.Sub(transfer.StartedAt).Seconds())
		transfer.TotalBytes, transfer.BytesTransferred = totalBytes, totalBytes
		transfer.Completed = true
	})
	return err
}

func (store *migrationStore) memorySnapshotDirectory(virtualMachineID string) string {
	return filepath.Join(store.migrationDirectory(virtualMachineID), "memory-snapshot")
}

func (m *Manager) restoreMemorySnapshot(ctx context.Context, record destinationRecord) error {
	directory := m.store.memorySnapshotDirectory(record.VirtualMachineID)
	var snapshot SourceSnapshot
	if err := readMigrationRecord(filepath.Join(directory, "disk-snapshot.json"), &snapshot); err != nil {
		return err
	}
	if snapshot.GUID == "" || snapshot.GUID != record.MemorySnapshotDiskGUID || snapshot.Sequence != lastCompletedSequence(record) {
		return errors.New("memory snapshot does not match the final disk checkpoint")
	}
	guid, err := m.disks.SnapshotGUID(ctx, record.VirtualMachineID, migrationSnapshotName(record.ID, snapshot.Sequence))
	if err != nil {
		return err
	}
	if guid != snapshot.GUID {
		return errors.New("memory snapshot does not match the received disk")
	}
	return m.host.RestoreMigratedMemorySnapshot(ctx, record.VirtualMachineID, directory)
}
