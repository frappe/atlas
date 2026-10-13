package migration

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"

	"github.com/frappe/atlas/metal/internal/vm"
)

// OpenSourceMemoryFile opens immutable state only after source cutover completes.
func (m *Manager) OpenSourceMemoryFile(ctx context.Context, migrationID, virtualMachineID string, sequence int, name string) (*os.File, error) {
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
	if record.State != sourceDetached || record.MigrateMemorySnapshot == nil || !*record.MigrateMemorySnapshot || sequence < 1 || sequence != record.FinalSequence {
		return nil, vm.ErrConflict
	}
	path := filepath.Join(m.host.VirtualMachineRecordsDirectory(), virtualMachineID, "saved-state", name)
	information, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !information.Mode().IsRegular() || information.Size() <= 0 {
		return nil, fmt.Errorf("invalid saved state file %s", name)
	}
	// The open descriptor remains valid if rollback or finish unlinks this file.
	return os.Open(path)
}

// ReceiveMemorySnapshot downloads the saved bundle into migration staging.
func (c *SourceClient) ReceiveMemorySnapshot(ctx context.Context, address, migrationID, virtualMachineID string, sequence int, directory string, memoryMiB int) error {
	for _, name := range []string{"state", "memory", "metadata.json"} {
		limit := int64(64 << 20)
		switch name {
		case "memory":
			limit = int64(memoryMiB) << 20
		case "metadata.json":
			limit = 1 << 20
		}
		if err := c.receiveMemoryFile(ctx, address, migrationID, virtualMachineID, sequence, directory, name, limit); err != nil {
			return fmt.Errorf("receive saved state %s: %w", name, err)
		}
	}
	return nil
}

func (c *SourceClient) receiveMemoryFile(ctx context.Context, address, migrationID, virtualMachineID string, sequence int, directory, name string, limit int64) error {
	path := sourcePath(migrationID, virtualMachineID, "/memory") + "&" + url.Values{
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
	file, err := os.OpenFile(filepath.Join(directory, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	hash := sha256.New()
	size, copyError := io.Copy(io.MultiWriter(file, hash), io.LimitReader(response.Body, limit+1))
	if err := errors.Join(copyError, file.Close()); err != nil {
		return err
	}
	if size <= 0 || size > limit {
		return fmt.Errorf("invalid file size %d (limit %d)", size, limit)
	}
	if hex.EncodeToString(hash.Sum(nil)) != response.Trailer.Get("X-Snapshot-SHA256") {
		return errors.New("snapshot checksum does not match the source")
	}
	return nil
}

func (m *Manager) receiveFinalSnapshots(ctx context.Context, record destinationRecord, final SourceSnapshot) error {
	if !record.MigrateMemorySnapshot {
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
	if record.MemorySnapshotGUID != "" {
		if record.MemorySnapshotGUID != final.GUID {
			return m.failTransfer(ctx, record, "memory snapshot belongs to a different disk snapshot")
		}
		return nil
	}
	desired, err := m.host.ReadDesired(record.VirtualMachineID)
	if err != nil {
		return err
	}
	published := m.store.memorySnapshotDirectory(record.VirtualMachineID)
	pending := published + "-pending"
	// ponytail: incomplete bundles restart; add byte ranges if retry cost matters.
	if err := os.RemoveAll(pending); err != nil {
		return err
	}
	if err := os.Mkdir(pending, 0o700); err != nil {
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
	var totalBytes int64
	for _, name := range []string{"state", "memory", "metadata.json"} {
		information, err := os.Stat(filepath.Join(pending, name))
		if err != nil {
			return err
		}
		totalBytes += information.Size()
	}
	if err := writeMigrationRecord(filepath.Join(pending, "disk-snapshot.json"), final); err != nil {
		return err
	}
	if err := os.RemoveAll(published); err != nil {
		return err
	}
	if err := os.Rename(pending, published); err != nil {
		return err
	}
	_, err = m.mutateDestination(context.WithoutCancel(ctx), record.VirtualMachineID, func(destination *destinationRecord) {
		destination.MemorySnapshotGUID = final.GUID
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
	if snapshot.GUID == "" || snapshot.GUID != record.MemorySnapshotGUID || snapshot.Sequence != lastCompletedSequence(record) {
		return errors.New("memory snapshot does not match the final disk checkpoint")
	}
	guid, err := m.disks.SnapshotGUID(ctx, record.VirtualMachineID, migrationSnapshotName(record.ID, snapshot.Sequence))
	if err != nil {
		return err
	}
	if guid != snapshot.GUID {
		return errors.New("memory snapshot does not match the received disk")
	}
	return m.host.RestoreMigratedMemory(ctx, record.VirtualMachineID, directory)
}
