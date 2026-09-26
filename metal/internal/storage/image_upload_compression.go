package storage

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/klauspost/compress/zstd"
)

// snapshotEncoderConcurrency limits compression CPU on hosts that run customer VMs.
const snapshotEncoderConcurrency = 1

// compressArtifact streams an artifact through zstd and uploads each part while
// it compresses the next.
func (store *SnapshotStore) compressArtifact(
	ctx context.Context,
	snapshotID, artifact string,
	source io.Reader,
	sizeBytes int64,
	request SnapshotArtifactUpload,
	stored []storedPart,
) (int64, []UploadedPart, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	chunker := &partChunker{
		store:         store,
		ctx:           ctx,
		snapshotID:    snapshotID,
		artifact:      artifact,
		uploadID:      request.UploadID,
		parts:         request.Parts,
		partSizeBytes: request.PartSizeBytes,
		stored:        stored,
		directory:     store.snapshotDirectory(snapshotID),
		bufferedParts: make(chan bufferedPart),
		uploadFailed:  make(chan struct{}),
		uploaderDone:  make(chan struct{}),
	}
	defer chunker.discard()

	go chunker.uploadParts()
	err := chunker.compress(source, sizeBytes)
	if err != nil {
		cancel()
	}
	close(chunker.bufferedParts)
	<-chunker.uploaderDone
	if err != nil {
		return 0, nil, err
	}
	if chunker.uploadError != nil {
		return 0, nil, chunker.uploadError
	}

	if chunker.skipped > 0 {
		store.logger.Info("resumed a snapshot upload",
			"snapshot_id", snapshotID, "artifact", artifact,
			"parts", len(chunker.uploadedParts), "skipped_parts", chunker.skipped)
	}
	return chunker.storedBytes, chunker.uploadedParts, nil
}

// partChunker writes compressed parts to disk and hands them to its uploader
// goroutine. The uploader owns the upload results until uploaderDone is closed.
type partChunker struct {
	store         *SnapshotStore
	ctx           context.Context
	snapshotID    string
	artifact      string
	uploadID      string
	parts         []SnapshotUploadPart
	partSizeBytes int64
	stored        []storedPart
	directory     string

	index   int
	current *os.File
	written int64

	bufferedParts chan bufferedPart
	uploadFailed  chan struct{}
	uploaderDone  chan struct{}
	uploadError   error

	storedBytes   int64
	uploadedParts []UploadedPart
	progress      []storedPart
	skipped       int
}

// bufferedPart is one full part on disk, waiting for upload.
type bufferedPart struct {
	part   SnapshotUploadPart
	file   *os.File
	length int64
}

func (part bufferedPart) remove() {
	part.file.Close()
	os.Remove(part.file.Name())
}

// compress writes the content size into the frame header, because a host
// sizes the ZFS volume from it before the download.
func (chunker *partChunker) compress(source io.Reader, sizeBytes int64) error {
	encoder, err := zstd.NewWriter(nil,
		zstd.WithEncoderLevel(zstd.SpeedFastest),
		zstd.WithEncoderConcurrency(snapshotEncoderConcurrency))
	if err != nil {
		return fmt.Errorf("create snapshot encoder: %w", err)
	}
	encoder.ResetContentSize(chunker, sizeBytes)
	if _, err := io.Copy(encoder, source); err != nil {
		encoder.Close()
		return err
	}
	if err := encoder.Close(); err != nil {
		return fmt.Errorf("finish snapshot encoder: %w", err)
	}
	if chunker.current == nil {
		return nil
	}
	return chunker.flush()
}

// Write buffers the compressed stream and flushes each full part.
func (chunker *partChunker) Write(data []byte) (int, error) {
	total := len(data)
	for len(data) > 0 {
		if chunker.current == nil {
			if err := chunker.open(); err != nil {
				return 0, err
			}
		}
		room := chunker.partSizeBytes - chunker.written
		chunk := data
		if int64(len(chunk)) > room {
			chunk = chunk[:room]
		}
		written, err := chunker.current.Write(chunk)
		chunker.written += int64(written)
		if err != nil {
			return total - len(data) + written, err
		}
		data = data[written:]
		if chunker.written >= chunker.partSizeBytes {
			if err := chunker.flush(); err != nil {
				return total - len(data), err
			}
		}
	}
	return total, nil
}

// open starts the next part file.
func (chunker *partChunker) open() error {
	if chunker.index >= len(chunker.parts) {
		return fmt.Errorf("%w: the compressed artifact needs more than the %d signed parts",
			ErrInvalidUpload, len(chunker.parts))
	}
	file, err := os.CreateTemp(chunker.directory, "part-*")
	if err != nil {
		return fmt.Errorf("create part buffer: %w", err)
	}
	chunker.current = file
	chunker.written = 0
	return nil
}

// flush hands the buffered part to the uploader and moves to the next one.
func (chunker *partChunker) flush() error {
	part := bufferedPart{part: chunker.parts[chunker.index], file: chunker.current, length: chunker.written}
	chunker.current = nil
	chunker.written = 0
	chunker.index++

	select {
	case chunker.bufferedParts <- part:
		return nil
	case <-chunker.uploadFailed:
		part.remove()
		return chunker.uploadError
	}
}

// uploadParts uploads parts in order and stops at the first failure.
func (chunker *partChunker) uploadParts() {
	defer close(chunker.uploaderDone)
	for part := range chunker.bufferedParts {
		err := chunker.uploadBufferedPart(part)
		part.remove()
		if err != nil {
			chunker.uploadError = err
			close(chunker.uploadFailed)
			return
		}
	}
}

func (chunker *partChunker) uploadBufferedPart(buffered bufferedPart) error {
	part := buffered.part
	etag, reused := chunker.reusableETag(part.PartNumber, buffered.length)
	if reused {
		chunker.skipped++
	}
	if !reused {
		if _, err := buffered.file.Seek(0, io.SeekStart); err != nil {
			return err
		}
		uploaded, err := chunker.store.uploadPartWithRetry(chunker.ctx, part, buffered.file, 0, buffered.length)
		if err != nil {
			return err
		}
		etag = uploaded
	}

	chunker.storedBytes += buffered.length
	chunker.uploadedParts = append(chunker.uploadedParts, UploadedPart{PartNumber: part.PartNumber, ETag: etag})
	chunker.progress = append(chunker.progress, storedPart{PartNumber: part.PartNumber, ETag: etag, SizeBytes: buffered.length})
	chunker.store.recordParts(chunker.snapshotID, chunker.artifact, chunker.uploadID, chunker.progress)
	return nil
}

// reusableETag returns a stored ETag only when the part length matches.
func (chunker *partChunker) reusableETag(partNumber int, length int64) (string, bool) {
	for _, candidate := range chunker.stored {
		if candidate.PartNumber == partNumber {
			return candidate.ETag, candidate.SizeBytes == length
		}
	}
	return "", false
}

// discard removes a part buffer left by a failure.
func (chunker *partChunker) discard() {
	if chunker.current == nil {
		return
	}
	path := chunker.current.Name()
	chunker.current.Close()
	os.Remove(path)
	chunker.current = nil
}

// removeStalePartBuffers deletes buffers left by a crashed upload.
func (store *SnapshotStore) removeStalePartBuffers(snapshotID string) {
	matches, err := filepath.Glob(filepath.Join(store.snapshotDirectory(snapshotID), "part-*"))
	if err != nil {
		return
	}
	for _, path := range matches {
		_ = os.Remove(path)
	}
}
