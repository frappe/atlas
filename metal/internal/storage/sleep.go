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

	"github.com/frappe/atlas/metal/internal/platform"
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
func (store *SnapshotStore) UploadSleepMemory(ctx context.Context, directory string, destination vm.SleepArtifactTarget, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
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
	return store.uploadSleepArtifact(ctx, archive.Name(), information.Size(), destination, directory, progress)
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
func (store *SnapshotStore) UploadSleepDisk(ctx context.Context, snapshotID string, destination vm.SleepArtifactTarget, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
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
		destination, store.snapshotDirectory(snapshotID), progress)
}

// uploadSleepArtifact compresses the artifact into its signed parts as it reads,
// so the host never holds a full compressed copy. One URL is a single part.
func (store *SnapshotStore) uploadSleepArtifact(ctx context.Context, path string, sizeBytes int64, destination vm.SleepArtifactTarget, directory string, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
	file, err := os.Open(path)
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	defer file.Close()

	request := SnapshotArtifactUpload{PartSizeBytes: maximumPartSizeBytes, Parts: []SnapshotUploadPart{{PartNumber: 1, URL: destination.UploadURL}}}
	if len(destination.Parts) > 0 {
		request = SnapshotArtifactUpload{PartSizeBytes: destination.PartSizeBytes}
		for index, part := range destination.Parts {
			request.Parts = append(request.Parts, SnapshotUploadPart{PartNumber: index + 1, URL: part.UploadURL})
		}
	}
	progress.TotalBytes.Store(sizeBytes)
	reader := newSHA256Reader(&countingReader{
		reader:  &sleepArtifactReader{context: ctx, source: io.NewSectionReader(file, 0, sizeBytes)},
		counter: &progress.UploadedBytes,
	})
	chunker := &partChunker{directory: directory}
	uploadError := store.compressParts(ctx, chunker, reader, sizeBytes, request)
	// Call Sum before the error check, so the hash goroutine always exits.
	digest := reader.Sum()
	if uploadError != nil {
		return vm.SleepArtifact{}, uploadError
	}

	artifact := vm.SleepArtifact{SizeBytes: sizeBytes, StoredSizeBytes: chunker.storedBytes, SHA256: hex.EncodeToString(digest)}
	if len(destination.Parts) > 0 {
		artifact.PartCount = len(chunker.uploadedParts)
	}
	return artifact, nil
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

// DownloadSleepMemory verifies an archive and extracts only the three saved-state files.
func (store *SnapshotStore) DownloadSleepMemory(ctx context.Context, directory string, sources []string, artifact vm.SleepArtifact) error {
	destination := &temporaryFileDestination{directory: directory}
	defer destination.discard()
	if err := downloadParts(ctx, store.httpClient, sources, artifact.SHA256, destination, store.logger); err != nil {
		return err
	}
	archive, err := os.Open(destination.file.Name())
	if err != nil {
		return err
	}
	defer archive.Close()
	information, err := archive.Stat()
	if err != nil {
		return err
	}
	if information.Size() != artifact.SizeBytes {
		return ErrImageIntegrity
	}
	return extractSleepMemory(ctx, archive, directory)
}

func extractSleepMemory(ctx context.Context, source io.Reader, directory string) error {
	archive := tar.NewReader(source)
	for _, name := range []string{"state", "memory", "metadata.json"} {
		header, err := archive.Next()
		if err != nil {
			return err
		}
		if header.Name != name || header.Typeflag != tar.TypeReg || header.Size <= 0 {
			return fmt.Errorf("invalid sleep archive entry %q", header.Name)
		}
		file, err := os.OpenFile(filepath.Join(directory, name), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		_, copyError := io.Copy(&SparseWriter{File: file}, &sleepArtifactReader{context: ctx, source: archive})
		err = errors.Join(copyError, file.Truncate(header.Size), file.Sync(), file.Close())
		if err != nil {
			return err
		}
	}
	if _, err := archive.Next(); !errors.Is(err, io.EOF) {
		return fmt.Errorf("unexpected data after sleep archive files")
	}
	return nil
}

// RestoreSleepDisk publishes a verified disk without replacing an existing VM disk.
func (store *SnapshotStore) RestoreSleepDisk(ctx context.Context, identifier string, sources []string, artifact vm.SleepArtifact) error {
	dataset := store.pool.virtualMachineDataset(identifier)
	exists, err := datasetExists(ctx, dataset)
	if err != nil || exists {
		return err
	}
	staging := store.pool.stagingDataset("sleep-" + identifier)
	volume := &volumeDestination{dataset: staging}
	// A crash can leave only the unpublished staging volume.
	volume.destroy()
	defer volume.destroy()
	if err := downloadParts(ctx, store.httpClient, sources, artifact.SHA256, volume, store.logger); err != nil {
		return err
	}
	return platform.Run(ctx, "zfs", "rename", staging, dataset)
}
