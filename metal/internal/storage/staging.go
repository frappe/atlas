package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/google/uuid"
)

var errInvalidStagedSnapshot = errors.New("invalid staged snapshot metadata")

// ArtifactSize contains the exact artifact size. The controller needs the byte
// count to plan a multipart upload.
type ArtifactSize struct {
	SizeBytes int64
}

// StagedSnapshot describes a local image staging snapshot.
type StagedSnapshot struct {
	ID                     string
	SourceVirtualMachineID string
	Rootfs                 ArtifactSize
	Kernel                 ArtifactSize
	Initrd                 ArtifactSize
}

// stagedSnapshotMetadata is the on-disk record of one staged snapshot. It
// survives a restart, so an upload can be reported and retried after one.
type stagedSnapshotMetadata struct {
	ID                     string `json:"id"`
	SourceVirtualMachineID string `json:"source_virtual_machine_id"`

	SourceSnapshot  string    `json:"source_snapshot"`
	RootfsSizeBytes int64     `json:"rootfs_size_bytes"`
	KernelSizeBytes int64     `json:"kernel_size_bytes"`
	InitrdSizeBytes int64     `json:"initrd_size_bytes,omitempty"`
	CreatedAt       time.Time `json:"created_at"`
	LastActivityAt  time.Time `json:"last_activity_at"`

	UploadState  string                `json:"upload_state,omitempty"`
	UploadError  string                `json:"upload_error,omitempty"`
	UploadResult *SnapshotUploadResult `json:"upload_result,omitempty"`

	RootfsProgress *artifactProgress `json:"rootfs_progress,omitempty"`
	KernelProgress *artifactProgress `json:"kernel_progress,omitempty"`
	InitrdProgress *artifactProgress `json:"initrd_progress,omitempty"`
}

// artifactProgress records parts already stored for a resumable upload.
type artifactProgress struct {
	UploadID string       `json:"upload_id"`
	Parts    []storedPart `json:"parts,omitempty"`
}

// storedPart records a stored part and the compressed length used to validate reuse.
type storedPart struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
	SizeBytes  int64  `json:"size_bytes"`
}

// storedParts returns progress for this multipart upload ID.
func (progress *artifactProgress) storedParts(uploadID string) []storedPart {
	if progress == nil || progress.UploadID == "" || progress.UploadID != uploadID {
		return nil
	}
	return progress.Parts
}

// Stage creates a staged snapshot for the VM manager.
func (store *SnapshotStore) Stage(ctx context.Context, request vm.SnapshotRequest) (vm.StagedSnapshot, error) {
	snapshotID, err := uuid.NewV7()
	if err != nil {
		return vm.StagedSnapshot{}, fmt.Errorf("generate snapshot identifier: %w", err)
	}
	staged, err := store.StageSnapshot(ctx, request.VirtualMachineID, snapshotID.String(), request.ImageReference, request.IsDiskEncrypted)
	if err != nil {
		return vm.StagedSnapshot{}, err
	}
	return vm.StagedSnapshot{
		ID:                     staged.ID,
		SourceVirtualMachineID: staged.SourceVirtualMachineID,
		RootfsSizeBytes:        staged.Rootfs.SizeBytes,
		KernelSizeBytes:        staged.Kernel.SizeBytes,
		InitrdSizeBytes:        staged.Initrd.SizeBytes,
	}, nil
}

// StageSnapshot creates a stable root file system clone and boot artifact links.
func (store *SnapshotStore) StageSnapshot(ctx context.Context, virtualMachineID, snapshotID, imageReference string, isDiskEncrypted bool) (StagedSnapshot, error) {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return StagedSnapshot{}, err
	}
	if found {
		if metadata.SourceVirtualMachineID != virtualMachineID {
			return StagedSnapshot{}, ErrInUse
		}
		return metadata.snapshot(), nil
	}

	return store.createStagedSnapshot(ctx, virtualMachineID, snapshotID, imageReference, isDiskEncrypted)
}

// createStagedSnapshot snapshots the live disk, clones it read-only, and stages
// its boot artifacts beside it. A failure attempts to destroy the clone and
// snapshot, so a retry normally starts clean.
func (store *SnapshotStore) createStagedSnapshot(ctx context.Context, virtualMachineID, snapshotID, imageReference string, isDiskEncrypted bool) (_ StagedSnapshot, resultError error) {
	sourceSnapshot := store.pool.snapshot(virtualMachineID, snapshotID)
	stagingDataset := store.pool.stagingDataset(snapshotID)
	directory := store.snapshotDirectory(snapshotID)

	if err := os.MkdirAll(directory, 0o750); err != nil {
		return StagedSnapshot{}, err
	}
	defer func() {
		if resultError != nil {
			_ = platform.Run(context.Background(), "zfs", "destroy", "-r", stagingDataset)
			_ = platform.Run(context.Background(), "zfs", "destroy", sourceSnapshot)
			_ = os.RemoveAll(directory)
		}
	}()

	if err := platform.Run(ctx, "zfs", "snapshot", sourceSnapshot); err != nil {
		return StagedSnapshot{}, fmt.Errorf("create source disk snapshot: %w", err)
	}
	if err := platform.Run(ctx, "zfs", "clone", "-o", "readonly=on", sourceSnapshot, stagingDataset); err != nil {
		return StagedSnapshot{}, fmt.Errorf("create staging disk clone: %w", err)
	}

	rootfsSize, err := volumeSizeBytes(ctx, stagingDataset)
	if err != nil {
		return StagedSnapshot{}, fmt.Errorf("read staging disk size: %w", err)
	}
	kernelSize, err := store.stageKernel(ctx, imageReference, filepath.Join(directory, "vmlinux"))
	if err != nil {
		return StagedSnapshot{}, err
	}
	manifest, found, err := store.images.loadImageManifest(imageReference)
	if err != nil {
		return StagedSnapshot{}, err
	}
	if isDiskEncrypted && (!found || manifest.InitrdSHA256 == "") {
		return StagedSnapshot{}, fmt.Errorf("%w: source image has no encryption initrd", ErrImageIntegrity)
	}
	var initrdSize int64
	if found && manifest.InitrdSHA256 != "" {
		initrdSize, err = store.stageArtifact(ctx, store.images.initrdFile(imageReference), filepath.Join(directory, "initrd"), "initrd")
		if err != nil {
			return StagedSnapshot{}, err
		}
	}

	now := time.Now().UTC()
	metadata := stagedSnapshotMetadata{
		ID:                     snapshotID,
		SourceVirtualMachineID: virtualMachineID,
		SourceSnapshot:         sourceSnapshot,
		RootfsSizeBytes:        rootfsSize,
		KernelSizeBytes:        kernelSize,
		InitrdSizeBytes:        initrdSize,
		CreatedAt:              now,
		LastActivityAt:         now,
	}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		return StagedSnapshot{}, fmt.Errorf("save staging metadata: %w", err)
	}

	return metadata.snapshot(), nil
}

// stageKernel places the image kernel beside the staged disk and returns its
// size. A hard link is used when the staging directory shares a file system.
func (store *SnapshotStore) stageKernel(ctx context.Context, imageReference, destination string) (int64, error) {
	return store.stageArtifact(ctx, store.images.kernelFile(imageReference), destination, "kernel")
}

func (store *SnapshotStore) stageArtifact(ctx context.Context, source, destination, name string) (int64, error) {
	if err := os.Link(source, destination); err != nil {
		if err := copyReflink(ctx, source, destination); err != nil {
			return 0, fmt.Errorf("stage %s: %w", name, err)
		}
	}

	information, err := os.Stat(destination)
	if err != nil {
		return 0, fmt.Errorf("read staged %s size: %w", name, err)
	}
	return information.Size(), nil
}

// DeleteSnapshot cancels any running upload, waits for it to stop, and then
// removes the staging clone, the source snapshot, and the staged files.
func (store *SnapshotStore) DeleteSnapshot(ctx context.Context, snapshotID string) error {
	store.cancelAndWaitUpload(snapshotID)

	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if errors.Is(err, errInvalidStagedSnapshot) {
		store.logger.Warn("remove staged snapshot with invalid metadata", "snapshot_id", snapshotID, "error", err)
		return store.deleteStagedSnapshotWithoutMetadata(ctx, snapshotID)
	}
	if err != nil {
		return err
	}
	if !found {
		return nil
	}

	return store.deleteStagedSnapshot(ctx, snapshotID, metadata)
}

// deleteStagedSnapshot removes the clone, the source snapshot, and the files.
// The clone goes first, because ZFS keeps a snapshot alive while a clone exists.
func (store *SnapshotStore) deleteStagedSnapshot(
	ctx context.Context,
	snapshotID string,
	metadata stagedSnapshotMetadata,
) error {
	if err := store.destroyIfPresent(ctx, store.pool.stagingDataset(snapshotID)); err != nil {
		return fmt.Errorf("remove staging disk: %w", err)
	}
	if err := store.destroyIfPresent(ctx, metadata.SourceSnapshot); err != nil {
		return fmt.Errorf("release source disk snapshot: %w", err)
	}
	if err := os.RemoveAll(store.snapshotDirectory(snapshotID)); err != nil {
		return fmt.Errorf("remove staging files: %w", err)
	}

	return nil
}

// deleteStagedSnapshotWithoutMetadata reads the clone origin from ZFS before
// it removes a staging record with corrupt metadata.
func (store *SnapshotStore) deleteStagedSnapshotWithoutMetadata(ctx context.Context, snapshotID string) error {
	origin, err := store.stagingOrigin(ctx, snapshotID)
	if err != nil {
		return err
	}
	if origin == "" {
		if err := store.destroyIfPresent(ctx, store.pool.stagingDataset(snapshotID)); err != nil {
			return fmt.Errorf("remove staging disk: %w", err)
		}
		if err := os.RemoveAll(store.snapshotDirectory(snapshotID)); err != nil {
			return fmt.Errorf("remove staging files: %w", err)
		}
		return nil
	}

	virtualMachineID := strings.TrimSuffix(
		strings.TrimPrefix(origin, store.pool.virtualMachineDataset("")),
		"@"+snapshotID,
	)
	recoveryTime := time.Unix(0, 0).UTC()
	metadata := stagedSnapshotMetadata{
		ID:                     snapshotID,
		SourceVirtualMachineID: virtualMachineID,
		SourceSnapshot:         origin,
		RootfsSizeBytes:        1,
		KernelSizeBytes:        1,
		CreatedAt:              recoveryTime,
		LastActivityAt:         recoveryTime,
	}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		return fmt.Errorf("save recovered staging metadata: %w", err)
	}
	return store.deleteStagedSnapshot(ctx, snapshotID, metadata)
}

// stagingOrigin returns the source snapshot of a staging clone. It accepts a
// clone that is already absent.
func (store *SnapshotStore) stagingOrigin(ctx context.Context, snapshotID string) (string, error) {
	output, err := store.runner.Output(
		ctx,
		"zfs",
		"get",
		"-Hp",
		"-o",
		"value",
		"origin",
		store.pool.stagingDataset(snapshotID),
	)
	if err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return "", nil
		}
		return "", fmt.Errorf("read staging disk origin: %w", err)
	}
	origin := strings.TrimSpace(output)
	if origin == "-" {
		return "", nil
	}
	expectedPrefix := store.pool.virtualMachineDataset("")
	virtualMachineID := strings.TrimSuffix(strings.TrimPrefix(origin, expectedPrefix), "@"+snapshotID)
	if !strings.HasPrefix(origin, expectedPrefix) ||
		!strings.HasSuffix(origin, "@"+snapshotID) ||
		!vm.ValidIdentifier(virtualMachineID) {
		return "", fmt.Errorf("staging disk returned invalid origin")
	}
	return origin, nil
}

// destroyIfPresent removes a dataset and accepts one that is already gone.
func (store *SnapshotStore) destroyIfPresent(ctx context.Context, dataset string) error {
	err := store.runner.Run(ctx, "zfs", "destroy", "-r", dataset)
	if err != nil && !strings.Contains(err.Error(), "does not exist") {
		return err
	}
	return nil
}

// destroyIfPresent removes a dataset and accepts one that is already gone.
func destroyIfPresent(ctx context.Context, dataset string) error {
	err := platform.Run(ctx, "zfs", "destroy", "-r", dataset)
	if err != nil && !strings.Contains(err.Error(), "does not exist") {
		return err
	}
	return nil
}

// loadStagedSnapshot reads and validates one staging record.
func (store *SnapshotStore) loadStagedSnapshot(snapshotID string) (stagedSnapshotMetadata, bool, error) {
	data, err := os.ReadFile(filepath.Join(store.snapshotDirectory(snapshotID), "metadata.json"))
	if errors.Is(err, os.ErrNotExist) {
		return stagedSnapshotMetadata{}, false, nil
	}
	if err != nil {
		return stagedSnapshotMetadata{}, false, err
	}

	var metadata stagedSnapshotMetadata
	if err := json.Unmarshal(data, &metadata); err != nil {
		return stagedSnapshotMetadata{}, false, fmt.Errorf("%w: %v", errInvalidStagedSnapshot, err)
	}
	if metadata.ID != snapshotID || metadata.SourceVirtualMachineID == "" || metadata.SourceSnapshot == "" || metadata.RootfsSizeBytes <= 0 || metadata.KernelSizeBytes <= 0 || metadata.CreatedAt.IsZero() || metadata.LastActivityAt.IsZero() {
		return stagedSnapshotMetadata{}, false, errInvalidStagedSnapshot
	}
	return metadata, true, nil
}

// saveStagedSnapshot replaces one staging record.
func (store *SnapshotStore) saveStagedSnapshot(metadata stagedSnapshotMetadata) error {
	path := filepath.Join(store.snapshotDirectory(metadata.ID), "metadata.json")
	return writeJSONFile(path, metadata, 0o640)
}

// PruneStagedSnapshots removes staging with no recent activity.
func (store *SnapshotStore) PruneStagedSnapshots(
	ctx context.Context,
	now time.Time,
	maximumIdle time.Duration,
) error {
	entries, err := os.ReadDir(store.directory)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if err := store.pruneStagedSnapshot(ctx, entry.Name(), now, maximumIdle); err != nil {
			return fmt.Errorf("prune snapshot %s: %w", entry.Name(), err)
		}
	}

	return nil
}

// pruneStagedSnapshot removes staging that has been idle past maximumIdle.
func (store *SnapshotStore) pruneStagedSnapshot(
	ctx context.Context,
	snapshotID string,
	now time.Time,
	maximumIdle time.Duration,
) error {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()
	if store.isUploadRunning(snapshotID) {
		return nil
	}

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if errors.Is(err, errInvalidStagedSnapshot) {
		store.logger.Warn("remove staged snapshot with invalid metadata", "snapshot_id", snapshotID, "error", err)
		return store.deleteStagedSnapshotWithoutMetadata(ctx, snapshotID)
	}
	if err != nil {
		return err
	}
	if !found || now.Sub(metadata.LastActivityAt) < maximumIdle {
		return nil
	}

	return store.deleteStagedSnapshot(ctx, snapshotID, metadata)
}

// snapshot returns the caller-facing view of one staging record.
func (metadata stagedSnapshotMetadata) snapshot() StagedSnapshot {
	return StagedSnapshot{
		ID:                     metadata.ID,
		SourceVirtualMachineID: metadata.SourceVirtualMachineID,
		Rootfs:                 ArtifactSize{SizeBytes: metadata.RootfsSizeBytes},
		Kernel:                 ArtifactSize{SizeBytes: metadata.KernelSizeBytes},
		Initrd:                 ArtifactSize{SizeBytes: metadata.InitrdSizeBytes},
	}
}

// writeJSONFile publishes one JSON document with an atomic rename.
func writeJSONFile(path string, value any, mode os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')

	return platform.WriteFile(path, data, mode)
}
