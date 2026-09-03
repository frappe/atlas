package storage

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPruneStagedSnapshotsRechecksActivityAfterLockWait(t *testing.T) {
	imagesDirectory := filepath.Join(t.TempDir(), "images")
	store := NewStores("test", imagesDirectory).Snapshots
	now := time.Now().UTC()
	metadata := stagedSnapshotMetadata{
		ID:                     "snapshot-1",
		SourceVirtualMachineID: "vm-1",
		SourceSnapshot:         "test/vms/vm-1@snapshot-1",
		RootfsSizeBytes:        1024,
		KernelSizeBytes:        512,
		CreatedAt:              now.Add(-49 * time.Hour),
		LastActivityAt:         now.Add(-49 * time.Hour),
	}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		t.Fatal(err)
	}

	lock := store.snapshotLock(metadata.ID)
	lock.Lock()
	pruneResult := make(chan error, 1)
	go func() {
		pruneResult <- store.PruneStagedSnapshots(t.Context(), now, 48*time.Hour)
	}()

	metadata.LastActivityAt = now
	if err := store.saveStagedSnapshot(metadata); err != nil {
		lock.Unlock()
		t.Fatal(err)
	}
	lock.Unlock()

	if err := <-pruneResult; err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(store.snapshotDirectory(metadata.ID), "metadata.json")); err != nil {
		t.Fatalf("active staging was removed: %v", err)
	}
}

func TestSnapshotUploadPartCountHasNoEmptyBoundaryPart(t *testing.T) {
	onePart := []SnapshotUploadPart{{PartNumber: 1, URL: "https://storage.example/part-1"}}
	if err := validateUploadParts(onePart, SnapshotPartSizeBytes); err != nil {
		t.Fatalf("exact part boundary: %v", err)
	}

	twoParts := append(onePart, SnapshotUploadPart{
		PartNumber: 2,
		URL:        "https://storage.example/part-2",
	})
	if err := validateUploadParts(twoParts, SnapshotPartSizeBytes+1); err != nil {
		t.Fatalf("part boundary plus one byte: %v", err)
	}
	if err := validateUploadParts(twoParts, SnapshotPartSizeBytes); err == nil {
		t.Fatal("extra empty part was accepted")
	}
}
