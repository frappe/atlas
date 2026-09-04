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

func TestUploadStatusReportsPersistedState(t *testing.T) {
	store := NewStores("test", filepath.Join(t.TempDir(), "images")).Snapshots

	if _, err := store.UploadStatus(t.Context(), "missing"); err != ErrNotFound {
		t.Fatalf("missing snapshot: got %v, want ErrNotFound", err)
	}

	now := time.Now().UTC()
	metadata := stagedSnapshotMetadata{
		ID:                     "snapshot-1",
		SourceVirtualMachineID: "vm-1",
		SourceSnapshot:         "test/vms/vm-1@snapshot-1",
		RootfsSizeBytes:        1024,
		KernelSizeBytes:        512,
		CreatedAt:              now,
		LastActivityAt:         now,
	}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		t.Fatal(err)
	}
	// An empty state reports "pending" so the controller starts the upload.
	status, err := store.UploadStatus(t.Context(), "snapshot-1")
	if err != nil {
		t.Fatal(err)
	}
	if status.State != UploadStatePending {
		t.Fatalf("empty state: got %q, want %q", status.State, UploadStatePending)
	}

	metadata.UploadState = UploadStateCompleted
	metadata.UploadResult = &SnapshotUploadResult{Rootfs: UploadedArtifact{SHA256: "abc"}}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		t.Fatal(err)
	}
	status, err = store.UploadStatus(t.Context(), "snapshot-1")
	if err != nil {
		t.Fatal(err)
	}
	if status.State != UploadStateCompleted || status.Result.Rootfs.SHA256 != "abc" {
		t.Fatalf("completed state not reported: %+v", status)
	}
	if status.UploadedBytes != status.TotalBytes || status.TotalBytes != 1536 {
		t.Fatalf("completed progress wrong: %+v", status)
	}

	// An "uploading" state with no running goroutine is an upload orphaned by a
	// host restart. It reports "pending" so the controller starts it again.
	metadata.UploadState = UploadStateUploading
	metadata.UploadResult = nil
	if err := store.saveStagedSnapshot(metadata); err != nil {
		t.Fatal(err)
	}
	status, err = store.UploadStatus(t.Context(), "snapshot-1")
	if err != nil {
		t.Fatal(err)
	}
	if status.State != UploadStatePending {
		t.Fatalf("orphaned upload not reported pending: %+v", status)
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
