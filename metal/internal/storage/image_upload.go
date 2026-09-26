package storage

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"
)

// Artifact names for the stored upload progress of one staged snapshot.
const (
	rootfsArtifact = "rootfs"
	kernelArtifact = "kernel"
)

const (
	// uploadPartAttempts retries transient failures without restarting the artifact.
	uploadPartAttempts = 3
	uploadRetryDelay   = 2 * time.Second

	minimumPartSizeBytes = 5 << 20
	maximumPartSizeBytes = 5 << 30
)

// errRetryableUpload marks a part failure worth repeating.
var errRetryableUpload = errors.New("retryable upload failure")

// SnapshotUploadPart is one presigned destination for a part of an artifact.
type SnapshotUploadPart struct {
	PartNumber int
	URL        string
}

// SnapshotArtifactUpload contains all upload parts for one artifact.
type SnapshotArtifactUpload struct {
	// UploadID identifies the multipart upload for resumable part progress.
	UploadID      string
	PartSizeBytes int64
	Parts         []SnapshotUploadPart
}

// SnapshotUploadRequest contains upload URLs for both image artifacts.
type SnapshotUploadRequest struct {
	Rootfs SnapshotArtifactUpload
	Kernel SnapshotArtifactUpload
}

// UploadedPart contains the ETag returned for one part.
type UploadedPart struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
}

// UploadedArtifact describes one uploaded artifact. SizeBytes is the
// uncompressed image and StoredSizeBytes is what the object store holds.
type UploadedArtifact struct {
	SizeBytes       int64          `json:"size_bytes"`
	StoredSizeBytes int64          `json:"stored_size_bytes"`
	SHA256          string         `json:"sha256"`
	Parts           []UploadedPart `json:"parts"`
}

// SnapshotUploadResult describes both uploaded image artifacts.
type SnapshotUploadResult struct {
	Rootfs UploadedArtifact `json:"rootfs"`
	Kernel UploadedArtifact `json:"kernel"`
}

// Snapshot upload states recorded in the staging metadata.
const (
	UploadStatePending   = "pending"
	UploadStateUploading = "uploading"
	UploadStateCompleted = "completed"
	UploadStateFailed    = "failed"
)

// SnapshotUploadStatus reports the upload progress of one staged snapshot.
type SnapshotUploadStatus struct {
	ID            string
	State         string
	UploadedBytes int64
	TotalBytes    int64
	Result        SnapshotUploadResult
	Error         string
}

// snapshotUpload lets deletion stop and wait for its upload goroutine.
type snapshotUpload struct {
	cancel   context.CancelFunc
	done     chan struct{}
	uploaded atomic.Int64
}

// StartUpload begins an asynchronous artifact upload and returns at once. It is
// idempotent: a call while an upload runs, or after it completes, does nothing.
func (store *SnapshotStore) StartUpload(_ context.Context, snapshotID string, request SnapshotUploadRequest) error {
	store.lifecycleMutex.Lock()
	if store.closed {
		store.lifecycleMutex.Unlock()
		return ErrShuttingDown
	}
	store.uploadsWaitGroup.Add(1)
	rootContext := store.rootContext
	store.lifecycleMutex.Unlock()

	// Release the shutdown counter unless a goroutine takes ownership.
	goroutineStarted := false
	defer func() {
		if !goroutineStarted {
			store.uploadsWaitGroup.Done()
		}
	}()

	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return err
	}
	if !found {
		return ErrNotFound
	}
	if metadata.UploadState == UploadStateCompleted {
		return nil
	}
	if err := validateUploadParts(request.Rootfs, metadata.RootfsSizeBytes); err != nil {
		return fmt.Errorf("rootfs parts: %w", err)
	}
	if err := validateUploadParts(request.Kernel, metadata.KernelSizeBytes); err != nil {
		return fmt.Errorf("kernel parts: %w", err)
	}

	uploadContext, cancel := context.WithCancel(rootContext)
	upload := &snapshotUpload{
		cancel: cancel,
		done:   make(chan struct{}),
	}

	store.uploadsMutex.Lock()
	if _, running := store.uploads[snapshotID]; running {
		store.uploadsMutex.Unlock()
		cancel()
		return nil
	}
	store.uploads[snapshotID] = upload
	store.uploadsMutex.Unlock()

	// Remove buffers left by a crashed upload.
	store.removeStalePartBuffers(snapshotID)

	metadata.UploadState = UploadStateUploading
	metadata.UploadError = ""
	metadata.LastActivityAt = time.Now().UTC()
	if err := store.saveStagedSnapshot(metadata); err != nil {
		cancel()
		store.removeUpload(snapshotID, upload)
		close(upload.done)
		return err
	}

	goroutineStarted = true
	go store.runUpload(uploadContext, snapshotID, upload, metadata.RootfsSizeBytes, metadata.KernelSizeBytes, request)

	return nil
}

// runUpload sends both artifacts and records the result.
func (store *SnapshotStore) runUpload(ctx context.Context, snapshotID string, upload *snapshotUpload, rootfsSize, kernelSize int64, request SnapshotUploadRequest) {
	defer func() {
		store.removeUpload(snapshotID, upload)
		close(upload.done)
		store.uploadsWaitGroup.Done()
	}()

	rootfs, err := store.uploadArtifact(ctx, snapshotID, rootfsArtifact,
		store.pool.stagingDevicePath(snapshotID), rootfsSize, request.Rootfs, &upload.uploaded)
	if err != nil {
		store.failUpload(ctx, snapshotID, fmt.Errorf("upload rootfs: %w", err))
		return
	}
	kernel, err := store.uploadArtifact(ctx, snapshotID, kernelArtifact,
		filepath.Join(store.snapshotDirectory(snapshotID), "vmlinux"), kernelSize, request.Kernel, &upload.uploaded)
	if err != nil {
		store.failUpload(ctx, snapshotID, fmt.Errorf("upload kernel: %w", err))
		return
	}
	store.finishUpload(snapshotID, SnapshotUploadResult{Rootfs: rootfs, Kernel: kernel})
}

// finishUpload records a completed upload and its result.
func (store *SnapshotStore) finishUpload(snapshotID string, result SnapshotUploadResult) {
	store.updateUploadMetadata(snapshotID, func(metadata *stagedSnapshotMetadata) {
		metadata.UploadState = UploadStateCompleted
		metadata.UploadError = ""
		metadata.UploadResult = &result
	})
}

// failUpload leaves cancelled uploads pending for retry.
func (store *SnapshotStore) failUpload(ctx context.Context, snapshotID string, cause error) {
	// Keep canceled uploads pending for retry.
	if ctx.Err() != nil {
		return
	}
	store.updateUploadMetadata(snapshotID, func(metadata *stagedSnapshotMetadata) {
		metadata.UploadState = UploadStateFailed
		metadata.UploadError = cause.Error()
	})
}

// updateUploadMetadata applies one change to the staging record under its lock.
func (store *SnapshotStore) updateUploadMetadata(snapshotID string, apply func(*stagedSnapshotMetadata)) {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil || !found {
		return
	}
	apply(&metadata)
	metadata.LastActivityAt = time.Now().UTC()
	_ = store.saveStagedSnapshot(metadata)
}

// removeUpload forgets an upload, unless a newer one already replaced it.
func (store *SnapshotStore) removeUpload(snapshotID string, upload *snapshotUpload) {
	store.uploadsMutex.Lock()
	defer store.uploadsMutex.Unlock()
	if store.uploads[snapshotID] == upload {
		delete(store.uploads, snapshotID)
	}
}

// cancelAndWaitUpload stops an upload before its staging data is removed.
func (store *SnapshotStore) cancelAndWaitUpload(snapshotID string) {
	store.uploadsMutex.Lock()
	upload := store.uploads[snapshotID]
	store.uploadsMutex.Unlock()
	if upload == nil {
		return
	}
	upload.cancel()
	<-upload.done
}

// isUploadRunning reports whether an upload goroutine owns the staging data.
func (store *SnapshotStore) isUploadRunning(snapshotID string) bool {
	store.uploadsMutex.Lock()
	defer store.uploadsMutex.Unlock()
	return store.uploads[snapshotID] != nil
}

// UploadStatus reports the current upload state of one staged snapshot. An
// upload recorded as running with no goroutine behind it did not survive a host
// restart, so it is reported as pending for the controller to start again.
func (store *SnapshotStore) UploadStatus(_ context.Context, snapshotID string) (SnapshotUploadStatus, error) {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return SnapshotUploadStatus{}, err
	}
	if !found {
		return SnapshotUploadStatus{}, ErrNotFound
	}
	state := metadata.UploadState
	if state == "" {
		state = UploadStatePending
	}
	status := SnapshotUploadStatus{
		ID:         snapshotID,
		TotalBytes: metadata.RootfsSizeBytes + metadata.KernelSizeBytes,
		Error:      metadata.UploadError,
	}
	if metadata.UploadResult != nil {
		status.Result = *metadata.UploadResult
	}

	if state == UploadStateCompleted {
		status.UploadedBytes = status.TotalBytes
	} else {
		store.uploadsMutex.Lock()
		upload := store.uploads[snapshotID]
		store.uploadsMutex.Unlock()
		if upload != nil {
			status.UploadedBytes = upload.uploaded.Load()
		} else if state == UploadStateUploading {
			// The upload goroutine did not survive a host restart. Report pending
			// so the controller starts the upload again.
			state = UploadStatePending
		}
	}
	status.State = state
	return status, nil
}

// isRetryableUploadStatus reports whether the object store may accept the part
// on another try.
func isRetryableUploadStatus(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests || status >= http.StatusInternalServerError
}

// storedParts returns the parts of one artifact the object store already holds.
func (store *SnapshotStore) storedParts(snapshotID, artifact, uploadID string) []storedPart {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()

	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil || !found {
		return nil
	}
	if artifact == rootfsArtifact {
		return metadata.RootfsProgress.storedParts(uploadID)
	}
	return metadata.KernelProgress.storedParts(uploadID)
}

// recordParts saves the parts stored so far. A failure only costs the resume,
// so it must not fail the upload that is working.
func (store *SnapshotStore) recordParts(snapshotID, artifact, uploadID string, parts []storedPart) {
	saved := make([]storedPart, len(parts))
	copy(saved, parts)
	progress := &artifactProgress{UploadID: uploadID, Parts: saved}
	store.updateUploadMetadata(snapshotID, func(metadata *stagedSnapshotMetadata) {
		if artifact == rootfsArtifact {
			metadata.RootfsProgress = progress
			return
		}
		metadata.KernelProgress = progress
	})
}

// validateUploadParts checks that the signed parts are numbered from 1 without
// gaps and carry usable URLs.
//
// The count is an upper bound, not an exact figure. The artifact is stored
// compressed, so how many parts it needs is known only once it is written. The
// controller signs enough parts for the uncompressed size and the upload uses
// as many as it needs.
func validateUploadParts(upload SnapshotArtifactUpload, sizeBytes int64) error {
	if sizeBytes <= 0 {
		return fmt.Errorf("%w: artifact size must be positive", ErrInvalidUpload)
	}
	if upload.PartSizeBytes < minimumPartSizeBytes || upload.PartSizeBytes > maximumPartSizeBytes {
		return fmt.Errorf("%w: part size must be from 5 MiB to 5 GiB", ErrInvalidUpload)
	}
	parts := upload.Parts
	if len(parts) == 0 {
		return fmt.Errorf("%w: at least one part is required", ErrInvalidUpload)
	}
	for index, part := range parts {
		if part.PartNumber != index+1 {
			return fmt.Errorf("%w: part numbers must be consecutive from 1", ErrInvalidUpload)
		}
		if _, err := parseImageURL(part.URL); err != nil {
			return fmt.Errorf("%w: part %d has an invalid URL", ErrInvalidUpload, part.PartNumber)
		}
	}
	return nil
}

// uploadArtifact sends one artifact part by part and digests it as it reads, so
// the file is read once for both the upload and the checksum.
func (store *SnapshotStore) uploadArtifact(ctx context.Context, snapshotID, artifact, path string, sizeBytes int64, request SnapshotArtifactUpload, uploaded *atomic.Int64) (UploadedArtifact, error) {
	file, err := os.Open(path)
	if err != nil {
		return UploadedArtifact{}, err
	}
	defer file.Close()

	// The digest covers the raw bytes, not the compressed parts.
	source := newSHA256Reader(&countingReader{
		reader:  io.NewSectionReader(file, 0, sizeBytes),
		counter: uploaded,
	})
	storedBytes, parts, err := store.compressArtifact(ctx, snapshotID, artifact, source, sizeBytes, request,
		store.storedParts(snapshotID, artifact, request.UploadID))
	// Call Sum before the error check, so the hash goroutine always exits.
	digest := source.Sum()
	if err != nil {
		return UploadedArtifact{}, err
	}

	return UploadedArtifact{
		SizeBytes:       sizeBytes,
		StoredSizeBytes: storedBytes,
		SHA256:          hex.EncodeToString(digest),
		Parts:           parts,
	}, nil
}

// countingReader reports progress against the uncompressed artifact, which is
// the size the controller shows.
type countingReader struct {
	reader  io.Reader
	counter *atomic.Int64
}

func (reader *countingReader) Read(buffer []byte) (int, error) {
	read, err := reader.reader.Read(buffer)
	if read > 0 {
		reader.counter.Add(int64(read))
	}
	return read, err
}

// uploadPartWithRetry sends one part, repeating a failure the object store may
// recover from. Each attempt reads the part again, so a partial body never
// reaches the next attempt.
func (store *SnapshotStore) uploadPartWithRetry(ctx context.Context, part SnapshotUploadPart, file *os.File, offset, length int64) (string, error) {
	var lastError error
	for attempt := range uploadPartAttempts {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(time.Duration(attempt) * uploadRetryDelay):
			}
		}
		etag, err := store.uploadPart(ctx, part, io.NewSectionReader(file, offset, length), length)
		if err == nil {
			return etag, nil
		}
		if ctx.Err() != nil || !errors.Is(err, errRetryableUpload) {
			return "", err
		}
		lastError = err
	}
	return "", lastError
}

// uploadPart sends one part and returns the ETag the destination reports.
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
		return "", fmt.Errorf("%w: upload part %d to %s failed", errRetryableUpload, part.PartNumber, redactURL(part.URL))
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		failure := fmt.Errorf("upload part %d to %s returned HTTP status %d", part.PartNumber, redactURL(part.URL), response.StatusCode)
		if isRetryableUploadStatus(response.StatusCode) {
			return "", fmt.Errorf("%w: %s", errRetryableUpload, failure)
		}
		return "", failure
	}
	etag := response.Header.Get("ETag")
	if etag == "" {
		return "", fmt.Errorf("upload part %d to %s returned no ETag", part.PartNumber, redactURL(part.URL))
	}
	return etag, nil
}
