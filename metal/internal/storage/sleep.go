package storage

import (
	"archive/tar"
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/klauspost/compress/zstd"

	"github.com/frappe/atlas/metal/internal/vm"
)

// StageSleepMemory copies immutable saved-state files before releasing the VM lock.
func (store *SnapshotStore) StageSleepMemory(ctx context.Context, source, directory string) error {
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	for _, name := range []string{"state", "memory", "metadata.json"} {
		if err := copyReflink(ctx, filepath.Join(source, name), filepath.Join(directory, name)); err != nil {
			return err
		}
	}
	return nil
}

// StageSleepDisk preserves the disk while a sleepy VM can wake during upload.
func (store *SnapshotStore) StageSleepDisk(ctx context.Context, request vm.SnapshotRequest, snapshotID string) error {
	_, err := store.StageSnapshot(ctx, request.VirtualMachineID, snapshotID, request.KernelSHA256)
	return err
}

// UploadSleepMemory uploads the three saved-state files as one compressed tar.
func (store *SnapshotStore) UploadSleepMemory(ctx context.Context, directory, destination string) (vm.SleepArtifact, error) {
	archive, err := os.CreateTemp(directory, "memory-*.tar")
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	defer os.Remove(archive.Name())
	defer archive.Close()
	writer := tar.NewWriter(archive)
	for _, name := range []string{"state", "memory", "metadata.json"} {
		if err := appendSleepFile(ctx, writer, directory, name); err != nil {
			return vm.SleepArtifact{}, err
		}
	}
	if err := writer.Close(); err != nil {
		return vm.SleepArtifact{}, err
	}
	information, err := archive.Stat()
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	return store.uploadSleepArtifact(ctx, archive.Name(), information.Size(), destination, directory)
}

func appendSleepFile(ctx context.Context, writer *tar.Writer, directory, name string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	file, err := os.Open(filepath.Join(directory, name))
	if err != nil {
		return err
	}
	defer file.Close()
	information, err := file.Stat()
	if err != nil {
		return err
	}
	if !information.Mode().IsRegular() || information.Size() <= 0 {
		return fmt.Errorf("invalid saved-state file %s", name)
	}
	if err := writer.WriteHeader(&tar.Header{Name: name, Mode: 0o600, Size: information.Size()}); err != nil {
		return err
	}
	_, err = io.Copy(writer, &sleepArtifactReader{context: ctx, source: file})
	return err
}

// UploadSleepDisk uploads the stable staging clone with the existing image encoding.
func (store *SnapshotStore) UploadSleepDisk(ctx context.Context, snapshotID, destination string) (vm.SleepArtifact, error) {
	lock := store.snapshotLock(snapshotID)
	lock.Lock()
	defer lock.Unlock()
	metadata, found, err := store.loadStagedSnapshot(snapshotID)
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	if !found {
		return vm.SleepArtifact{}, ErrNotFound
	}
	return store.uploadSleepArtifact(ctx, store.pool.stagingDevicePath(snapshotID), metadata.RootfsSizeBytes,
		destination, store.snapshotDirectory(snapshotID))
}

func (store *SnapshotStore) uploadSleepArtifact(ctx context.Context, path string, sizeBytes int64, destination, directory string) (vm.SleepArtifact, error) {
	source, err := os.Open(path)
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	defer source.Close()
	compressed, err := os.CreateTemp(directory, "sleep-upload-*")
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	defer os.Remove(compressed.Name())
	defer compressed.Close()
	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedFastest), zstd.WithEncoderConcurrency(snapshotEncoderConcurrency))
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	encoder.ResetContentSize(compressed, sizeBytes)
	reader := newSHA256Reader(&sleepArtifactReader{context: ctx, source: io.NewSectionReader(source, 0, sizeBytes)})
	_, copyError := io.Copy(encoder, reader)
	digest := reader.Sum()
	if err := errors.Join(copyError, encoder.Close()); err != nil {
		return vm.SleepArtifact{}, err
	}
	information, err := compressed.Stat()
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	if _, err := store.uploadPartWithRetry(ctx, SnapshotUploadPart{PartNumber: 1, URL: destination}, compressed, 0, information.Size()); err != nil {
		return vm.SleepArtifact{}, err
	}
	return vm.SleepArtifact{SizeBytes: sizeBytes, StoredSizeBytes: information.Size(), SHA256: hex.EncodeToString(digest)}, nil
}

type sleepArtifactReader struct {
	context context.Context
	source  io.Reader
}

func (reader *sleepArtifactReader) Read(data []byte) (int, error) {
	if err := reader.context.Err(); err != nil {
		return 0, err
	}
	return reader.source.Read(data)
}
