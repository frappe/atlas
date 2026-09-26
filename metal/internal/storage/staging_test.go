package storage

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type stagingRunner struct {
	origin        string
	destroyed     []string
	destroyErrors map[string]error
}

func (runner *stagingRunner) Run(_ context.Context, _ string, arguments ...string) error {
	if len(arguments) == 3 && arguments[0] == "destroy" {
		runner.destroyed = append(runner.destroyed, arguments[2])
		return runner.destroyErrors[arguments[2]]
	}
	return nil
}

func (runner *stagingRunner) Output(context.Context, string, ...string) (string, error) {
	return runner.origin, nil
}

func TestSnapshotStoreShutdownRejectsNewUploads(t *testing.T) {
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots
	shutdownContext, cancel := context.WithCancel(t.Context())
	defer cancel()
	if err := store.Shutdown(shutdownContext); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if err := store.StartUpload(t.Context(), "snapshot-1", SnapshotUploadRequest{}); err == nil {
		t.Fatal("upload started after shutdown")
	}
}

func TestPruneStagedSnapshotsRechecksActivityAfterLockWait(t *testing.T) {
	imagesDirectory := filepath.Join(t.TempDir(), "images")
	store := NewStores(t.Context(), "test", imagesDirectory, nil).Snapshots
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

func TestPruneStagedSnapshotsKeepsAStagingCloneDuringUpload(t *testing.T) {
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots
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
	store.uploads[metadata.ID] = &snapshotUpload{}

	if err := store.PruneStagedSnapshots(t.Context(), now, 48*time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(store.snapshotDirectory(metadata.ID), "metadata.json")); err != nil {
		t.Fatalf("active upload staging was removed: %v", err)
	}
}

func TestPruneStagedSnapshotsRemovesInvalidMetadata(t *testing.T) {
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots
	runner := &stagingRunner{origin: "test/vms/vm-1@snapshot-1\n"}
	store.runner = runner
	directory := store.snapshotDirectory("snapshot-1")
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "metadata.json"), []byte("not json"), 0o640); err != nil {
		t.Fatal(err)
	}

	if err := store.PruneStagedSnapshots(t.Context(), time.Now().UTC(), 48*time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(directory); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("invalid staging directory still exists: %v", err)
	}
	if got := strings.Join(runner.destroyed, ","); got != "test/staging/snapshot-1,test/vms/vm-1@snapshot-1" {
		t.Fatalf("destroyed = %q", got)
	}
}

func TestInvalidMetadataCleanupCanRetryAfterSourceRemovalFails(t *testing.T) {
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots
	sourceSnapshot := "test/vms/vm-1@snapshot-1"
	runner := &stagingRunner{
		origin:        sourceSnapshot + "\n",
		destroyErrors: map[string]error{sourceSnapshot: errors.New("source busy")},
	}
	store.runner = runner
	directory := store.snapshotDirectory("snapshot-1")
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "metadata.json"), []byte("not json"), 0o640); err != nil {
		t.Fatal(err)
	}

	if err := store.PruneStagedSnapshots(t.Context(), time.Now().UTC(), 48*time.Hour); err == nil {
		t.Fatal("cleanup succeeded while source snapshot removal failed")
	}
	if _, _, err := store.loadStagedSnapshot("snapshot-1"); err != nil {
		t.Fatalf("recovery metadata was not saved: %v", err)
	}
	delete(runner.destroyErrors, sourceSnapshot)
	if err := store.PruneStagedSnapshots(t.Context(), time.Now().UTC(), 48*time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(directory); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("staging directory still exists after retry: %v", err)
	}
}

func TestUploadStatusReportsPersistedState(t *testing.T) {
	store := NewStores(t.Context(), "test", filepath.Join(t.TempDir(), "images"), nil).Snapshots

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

func TestSnapshotUploadPartsAreAnUpperBound(t *testing.T) {
	const partSizeBytes = 64 << 20
	onePart := SnapshotArtifactUpload{PartSizeBytes: partSizeBytes, Parts: []SnapshotUploadPart{{PartNumber: 1, URL: "https://storage.example/part-1"}}}
	if err := validateUploadParts(onePart, partSizeBytes); err != nil {
		t.Fatalf("exact part boundary: %v", err)
	}

	twoParts := onePart
	twoParts.Parts = append(onePart.Parts, SnapshotUploadPart{
		PartNumber: 2,
		URL:        "https://storage.example/part-2",
	})
	// The artifact is stored compressed, so it usually needs fewer parts than
	// its uncompressed size would take. A spare signed part is expected.
	if err := validateUploadParts(twoParts, partSizeBytes); err != nil {
		t.Fatalf("spare part rejected: %v", err)
	}

	if err := validateUploadParts(SnapshotArtifactUpload{PartSizeBytes: partSizeBytes}, partSizeBytes); err == nil {
		t.Fatal("an artifact with no signed part was accepted")
	}
	gap := SnapshotArtifactUpload{PartSizeBytes: partSizeBytes, Parts: []SnapshotUploadPart{{PartNumber: 2, URL: "https://storage.example/part-2"}}}
	if err := validateUploadParts(gap, partSizeBytes); err == nil {
		t.Fatal("parts that do not start at 1 were accepted")
	}
	tooSmall := onePart
	tooSmall.PartSizeBytes = 1 << 20
	if err := validateUploadParts(tooSmall, partSizeBytes); err == nil {
		t.Fatal("a part size below the object store minimum was accepted")
	}
}
