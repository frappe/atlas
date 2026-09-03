package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// SnapshotPartSizeBytes is the fixed multipart upload size.
const SnapshotPartSizeBytes int64 = 2 << 30

// ArtifactSize contains the exact artifact size.
type ArtifactSize struct {
	SizeBytes int64
}

// StagedSnapshot describes a local image staging snapshot.
type StagedSnapshot struct {
	ID                     string
	SourceVirtualMachineID string
	Rootfs                 ArtifactSize
	Kernel                 ArtifactSize
}

// SnapshotUploadPart identifies one signed multipart upload URL.
type SnapshotUploadPart struct {
	PartNumber int
	URL        string
}

// SnapshotArtifactUpload contains all upload parts for one artifact.
type SnapshotArtifactUpload struct {
	Parts []SnapshotUploadPart
}

// SnapshotUploadRequest contains upload URLs for both image artifacts.
type SnapshotUploadRequest struct {
	Rootfs SnapshotArtifactUpload
	Kernel SnapshotArtifactUpload
}

// UploadedPart contains the ETag returned for one part.
type UploadedPart struct {
	PartNumber int
	ETag       string
}

// UploadedArtifact describes one uploaded artifact.
type UploadedArtifact struct {
	SizeBytes int64
	SHA256    string
	Parts     []UploadedPart
}

// SnapshotUploadResult describes both uploaded image artifacts.
type SnapshotUploadResult struct {
	Rootfs UploadedArtifact
	Kernel UploadedArtifact
}

type stagedSnapshotMetadata struct {
	ID                     string `json:"id"`
	SourceVirtualMachineID string `json:"source_virtual_machine_id"`

	SourceSnapshot  string    `json:"source_snapshot"`
	RootfsSizeBytes int64     `json:"rootfs_size_bytes"`
	KernelSizeBytes int64     `json:"kernel_size_bytes"`
	CreatedAt       time.Time `json:"created_at"`
	LastActivityAt  time.Time `json:"last_activity_at"`
}

// StageSnapshot creates a stable root file system clone and kernel link.
func (store *SnapshotStore) StageSnapshot(ctx context.Context, virtualMachineID, snapshotID, imageReference string) (StagedSnapshot, error) {
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

	return store.createStagedSnapshot(ctx, virtualMachineID, snapshotID, imageReference)
}

func (store *SnapshotStore) createStagedSnapshot(ctx context.Context, virtualMachineID, snapshotID, imageReference string) (_ StagedSnapshot, resultError error) {
	sourceSnapshot := store.pool.snapshot(virtualMachineID, snapshotID)
	stagingDataset := store.pool.stagingDataset(snapshotID)
	directory := store.snapshotDirectory(snapshotID)

	if err := os.MkdirAll(directory, 0o750); err != nil {
		return StagedSnapshot{}, err
	}
	defer func() {
		if resultError != nil {
			_ = hostcmd.Run(context.Background(), "zfs", "destroy", "-r", stagingDataset)
			_ = hostcmd.Run(context.Background(), "zfs", "destroy", sourceSnapshot)
			_ = os.RemoveAll(directory)
		}
	}()

	if err := hostcmd.Run(ctx, "zfs", "snapshot", sourceSnapshot); err != nil {
		return StagedSnapshot{}, fmt.Errorf("create source disk snapshot: %w", err)
	}
	if err := hostcmd.Run(ctx, "zfs", "clone", "-o", "readonly=on", sourceSnapshot, stagingDataset); err != nil {
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

	now := time.Now().UTC()
	metadata := stagedSnapshotMetadata{
		ID:                     snapshotID,
		SourceVirtualMachineID: virtualMachineID,
		SourceSnapshot:         sourceSnapshot,
		RootfsSizeBytes:        rootfsSize,
		KernelSizeBytes:        kernelSize,
		CreatedAt:              now,
		LastActivityAt:         now,
	}
	if err := store.saveStagedSnapshot(metadata); err != nil {
		return StagedSnapshot{}, fmt.Errorf("save staging metadata: %w", err)
	}

	return metadata.snapshot(), nil
}

func (store *SnapshotStore) stageKernel(ctx context.Context, imageReference, destination string) (int64, error) {
	source := store.images.kernelFile(imageReference)
	if err := os.Link(source, destination); err != nil {
		if err := copyReflink(ctx, source, destination); err != nil {
			return 0, fmt.Errorf("stage kernel: %w", err)
		}
	}

	information, err := os.Stat(destination)
	if err != nil {
		return 0, fmt.Errorf("read staged kernel size: %w", err)
	}
	return information.Size(), nil
}

// UploadSnapshot streams all requested artifact parts in order.
func (store *SnapshotStore) UploadSnapshot(ctx context.Context, snapshotID string, request SnapshotUploadRequest) (SnapshotUploadResult, error) {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return SnapshotUploadResult{}, err
	}
	if !found {
		return SnapshotUploadResult{}, ErrNotFound
	}
	metadata.LastActivityAt = time.Now().UTC()
	if err := store.saveStagedSnapshot(metadata); err != nil {
		return SnapshotUploadResult{}, err
	}
	if err := validateUploadParts(request.Rootfs.Parts, metadata.RootfsSizeBytes); err != nil {
		return SnapshotUploadResult{}, fmt.Errorf("rootfs parts: %w", err)
	}
	if err := validateUploadParts(request.Kernel.Parts, metadata.KernelSizeBytes); err != nil {
		return SnapshotUploadResult{}, fmt.Errorf("kernel parts: %w", err)
	}

	rootfs, err := store.uploadArtifact(ctx, store.pool.stagingDevicePath(snapshotID), metadata.RootfsSizeBytes, request.Rootfs.Parts)
	if err != nil {
		return SnapshotUploadResult{}, fmt.Errorf("upload rootfs: %w", err)
	}
	kernel, err := store.uploadArtifact(ctx, filepath.Join(store.snapshotDirectory(snapshotID), "vmlinux"), metadata.KernelSizeBytes, request.Kernel.Parts)
	if err != nil {
		return SnapshotUploadResult{}, fmt.Errorf("upload kernel: %w", err)
	}

	metadata.LastActivityAt = time.Now().UTC()
	if err := store.saveStagedSnapshot(metadata); err != nil {
		return SnapshotUploadResult{}, err
	}
	return SnapshotUploadResult{Rootfs: rootfs, Kernel: kernel}, nil
}

func validateUploadParts(parts []SnapshotUploadPart, sizeBytes int64) error {
	if sizeBytes <= 0 {
		return fmt.Errorf("artifact size must be positive")
	}
	expectedCount := int((sizeBytes + SnapshotPartSizeBytes - 1) / SnapshotPartSizeBytes)
	if len(parts) != expectedCount {
		return fmt.Errorf("expected %d parts", expectedCount)
	}
	for index, part := range parts {
		if part.PartNumber != index+1 {
			return fmt.Errorf("part numbers must be consecutive from 1")
		}
		if _, err := parseImageURL(part.URL); err != nil {
			return fmt.Errorf("part %d has an invalid URL", part.PartNumber)
		}
	}
	return nil
}

func (store *SnapshotStore) uploadArtifact(ctx context.Context, path string, sizeBytes int64, parts []SnapshotUploadPart) (UploadedArtifact, error) {
	file, err := os.Open(path)
	if err != nil {
		return UploadedArtifact{}, err
	}
	defer file.Close()

	digest := sha256.New()
	uploadedParts := make([]UploadedPart, 0, len(parts))
	for index, part := range parts {
		offset := int64(index) * SnapshotPartSizeBytes
		length := min(SnapshotPartSizeBytes, sizeBytes-offset)
		reader := io.TeeReader(io.NewSectionReader(file, offset, length), digest)

		etag, err := store.uploadPart(ctx, part, reader, length)
		if err != nil {
			return UploadedArtifact{}, err
		}
		uploadedParts = append(uploadedParts, UploadedPart{PartNumber: part.PartNumber, ETag: etag})
	}

	return UploadedArtifact{
		SizeBytes: sizeBytes,
		SHA256:    hex.EncodeToString(digest.Sum(nil)),
		Parts:     uploadedParts,
	}, nil
}

func (store *SnapshotStore) uploadPart(ctx context.Context, part SnapshotUploadPart, body io.Reader, sizeBytes int64) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, part.URL, body)
	if err != nil {
		return "", fmt.Errorf("create part %d request", part.PartNumber)
	}
	request.ContentLength = sizeBytes

	response, err := store.httpClient.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("upload part %d to %s failed", part.PartNumber, redactURL(part.URL))
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return "", fmt.Errorf("upload part %d to %s returned HTTP status %d", part.PartNumber, redactURL(part.URL), response.StatusCode)
	}
	etag := response.Header.Get("ETag")
	if etag == "" {
		return "", fmt.Errorf("upload part %d to %s returned no ETag", part.PartNumber, redactURL(part.URL))
	}
	return etag, nil
}

// DeleteSnapshot removes staging before it releases the source disk snapshot.
func (store *SnapshotStore) DeleteSnapshot(ctx context.Context, snapshotID string) error {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return err
	}
	if !found {
		return nil
	}

	return store.deleteStagedSnapshot(ctx, snapshotID, metadata)
}

func (store *SnapshotStore) deleteStagedSnapshot(
	ctx context.Context,
	snapshotID string,
	metadata stagedSnapshotMetadata,
) error {
	if err := destroyIfPresent(ctx, store.pool.stagingDataset(snapshotID)); err != nil {
		return fmt.Errorf("remove staging disk: %w", err)
	}
	if err := destroyIfPresent(ctx, metadata.SourceSnapshot); err != nil {
		return fmt.Errorf("release source disk snapshot: %w", err)
	}
	if err := os.RemoveAll(store.snapshotDirectory(snapshotID)); err != nil {
		return fmt.Errorf("remove staging files: %w", err)
	}

	return nil
}

func destroyIfPresent(ctx context.Context, dataset string) error {
	err := hostcmd.Run(ctx, "zfs", "destroy", "-r", dataset)
	if err != nil && !strings.Contains(err.Error(), "does not exist") {
		return err
	}
	return nil
}

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
		return stagedSnapshotMetadata{}, false, fmt.Errorf("decode staging metadata: %w", err)
	}
	if metadata.ID != snapshotID || metadata.SourceVirtualMachineID == "" || metadata.SourceSnapshot == "" || metadata.RootfsSizeBytes <= 0 || metadata.KernelSizeBytes <= 0 || metadata.CreatedAt.IsZero() || metadata.LastActivityAt.IsZero() {
		return stagedSnapshotMetadata{}, false, fmt.Errorf("staging metadata is invalid")
	}
	return metadata, true, nil
}

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

func (store *SnapshotStore) pruneStagedSnapshot(
	ctx context.Context,
	snapshotID string,
	now time.Time,
	maximumIdle time.Duration,
) error {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return err
	}
	if !found || now.Sub(metadata.LastActivityAt) < maximumIdle {
		return nil
	}

	return store.deleteStagedSnapshot(ctx, snapshotID, metadata)
}

func (metadata stagedSnapshotMetadata) snapshot() StagedSnapshot {
	return StagedSnapshot{
		ID:                     metadata.ID,
		SourceVirtualMachineID: metadata.SourceVirtualMachineID,
		Rootfs:                 ArtifactSize{SizeBytes: metadata.RootfsSizeBytes},
		Kernel:                 ArtifactSize{SizeBytes: metadata.KernelSizeBytes},
	}
}

func writeJSONFile(path string, value any, mode os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')

	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return err
	}
	temporaryFile, err := os.CreateTemp(directory, ".metadata-*")
	if err != nil {
		return err
	}
	temporaryPath := temporaryFile.Name()
	defer os.Remove(temporaryPath)

	if err := temporaryFile.Chmod(mode); err != nil {
		temporaryFile.Close()
		return err
	}
	if _, err := temporaryFile.Write(data); err != nil {
		temporaryFile.Close()
		return err
	}
	if err := temporaryFile.Sync(); err != nil {
		temporaryFile.Close()
		return err
	}
	if err := temporaryFile.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}

	directoryFile, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer directoryFile.Close()
	return directoryFile.Sync()
}
