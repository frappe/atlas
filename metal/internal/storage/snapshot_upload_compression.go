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

// compressArtifact streams an artifact through zstd and uploads its parts.
func (store *SnapshotStore) compressArtifact(
	ctx context.Context,
	snapshotID, artifact string,
	source io.Reader,
	request SnapshotArtifactUpload,
	stored []storedPart,
) (int64, []UploadedPart, error) {
	chunker := &partChunker{
		store:      store,
		ctx:        ctx,
		snapshotID: snapshotID,
		artifact:   artifact,
		uploadID:   request.UploadID,
		parts:      request.Parts,
		stored:     stored,
		directory:  store.snapshotDirectory(snapshotID),
	}
	defer chunker.discard()

	encoder, err := zstd.NewWriter(chunker,
		zstd.WithEncoderLevel(zstd.SpeedFastest),
		zstd.WithEncoderConcurrency(snapshotEncoderConcurrency))
	if err != nil {
		return 0, nil, fmt.Errorf("create snapshot encoder: %w", err)
	}
	if _, err := io.Copy(encoder, source); err != nil {
		encoder.Close()
		return 0, nil, err
	}
	if err := encoder.Close(); err != nil {
		return 0, nil, fmt.Errorf("finish snapshot encoder: %w", err)
	}
	if err := chunker.finish(); err != nil {
		return 0, nil, err
	}
	if chunker.skipped > 0 {
		store.logger.Info("resumed a snapshot upload",
			"snapshot_id", snapshotID, "artifact", artifact,
			"parts", len(chunker.uploadedParts), "skipped_parts", chunker.skipped)
	}
	return chunker.storedBytes, chunker.uploadedParts, nil
}

// partChunker stores and uploads one compressed part at a time.
type partChunker struct {
	store      *SnapshotStore
	ctx        context.Context
	snapshotID string
	artifact   string
	uploadID   string
	parts      []SnapshotUploadPart
	stored     []storedPart
	directory  string

	index         int
	current       *os.File
	written       int64
	storedBytes   int64
	uploadedParts []UploadedPart
	progress      []storedPart
	skipped       int
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
		room := SnapshotPartSizeBytes - chunker.written
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
		if chunker.written >= SnapshotPartSizeBytes {
			if err := chunker.flush(); err != nil {
				return total - len(data), err
			}
		}
	}
	return total, nil
}

// finish uploads the remainder of the stream.
func (chunker *partChunker) finish() error {
	if chunker.current == nil {
		return nil
	}
	return chunker.flush()
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

// flush stores the buffered part and moves to the next one.
func (chunker *partChunker) flush() error {
	part := chunker.parts[chunker.index]
	path := chunker.current.Name()
	length := chunker.written
	defer func() {
		chunker.current.Close()
		os.Remove(path)
		chunker.current = nil
		chunker.written = 0
		chunker.index++
	}()

	etag, reused := chunker.reusableETag(part.PartNumber, length)
	if reused {
		chunker.skipped++
	}
	if !reused {
		if _, err := chunker.current.Seek(0, io.SeekStart); err != nil {
			return err
		}
		uploaded, err := chunker.store.uploadPartWithRetry(chunker.ctx, part, chunker.current, 0, length)
		if err != nil {
			return err
		}
		etag = uploaded
	}

	chunker.storedBytes += length
	chunker.uploadedParts = append(chunker.uploadedParts, UploadedPart{PartNumber: part.PartNumber, ETag: etag})
	chunker.progress = append(chunker.progress, storedPart{PartNumber: part.PartNumber, ETag: etag, SizeBytes: length})
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
