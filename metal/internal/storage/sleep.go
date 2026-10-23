package storage

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
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
func (store *SnapshotStore) UploadSleepMemory(ctx context.Context, directory string, target vm.SleepArtifactTarget, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
	sizeBytes, err := sleepArchiveSize(directory)
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	reader, writer := io.Pipe()
	done := make(chan struct{})
	go func() {
		defer close(done)
		archive := tar.NewWriter(writer)
		for _, name := range []string{"state", "memory", "metadata.json"} {
			if err := appendSleepFile(ctx, archive, directory, name); err != nil {
				writer.CloseWithError(err)
				return
			}
		}
		writer.CloseWithError(archive.Close())
	}()
	defer func() { reader.Close(); <-done }()
	return store.uploadSleepArtifact(ctx, reader, sizeBytes, target, directory, progress)
}

func sleepArchiveSize(directory string) (int64, error) {
	size := int64(1024) // tar end markers
	for _, name := range []string{"state", "memory", "metadata.json"} {
		information, err := os.Stat(filepath.Join(directory, name))
		if err != nil {
			return 0, err
		}
		if !information.Mode().IsRegular() || information.Size() <= 0 {
			return 0, fmt.Errorf("invalid saved-state file %s", name)
		}
		// Let archive/tar account for extended headers on large memory files.
		var header bytes.Buffer
		if err := tar.NewWriter(&header).WriteHeader(&tar.Header{Name: name, Mode: 0o600, Size: information.Size()}); err != nil {
			return 0, err
		}
		size += int64(header.Len()) + (information.Size()+511)/512*512
	}
	return size, nil
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
func (store *SnapshotStore) UploadSleepDisk(ctx context.Context, snapshotID string, target vm.SleepArtifactTarget, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
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
	source, err := os.Open(store.pool.stagingDevicePath(snapshotID))
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	defer source.Close()
	return store.uploadSleepArtifact(ctx, io.NewSectionReader(source, 0, metadata.RootfsSizeBytes), metadata.RootfsSizeBytes,
		target, store.snapshotDirectory(snapshotID), progress)
}

func (store *SnapshotStore) uploadSleepArtifact(ctx context.Context, source io.Reader, sizeBytes int64, target vm.SleepArtifactTarget, directory string, progress *vm.SleepUploadProgress) (vm.SleepArtifact, error) {
	if target.Multipart == nil {
		return vm.SleepArtifact{}, ErrInvalidUpload
	}
	plan := target.Multipart
	request := SnapshotArtifactUpload{UploadID: plan.UploadID, PartSizeBytes: plan.PartSizeMiB << 20}
	for _, part := range plan.Parts {
		request.Parts = append(request.Parts, SnapshotUploadPart{PartNumber: part.PartNumber, URL: part.URL})
	}
	if err := validateUploadParts(request, sizeBytes); err != nil {
		return vm.SleepArtifact{}, err
	}
	store.removeStalePartBuffers(directory)
	path := filepath.Join(directory, "sleep-parts.json")
	var saved artifactProgress
	data, err := os.ReadFile(path)
	if err == nil {
		if err := json.Unmarshal(data, &saved); err != nil {
			return vm.SleepArtifact{}, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return vm.SleepArtifact{}, err
	}
	reader := newSHA256Reader(&sleepArtifactReader{context: ctx, source: source})
	storedBytes, parts, err := store.compressArtifact(ctx, reader, sizeBytes, request, directory, saved.storedParts(plan.UploadID), func(parts []storedPart) error {
		data, err := json.Marshal(artifactProgress{UploadID: plan.UploadID, Parts: parts})
		if err != nil {
			return err
		}
		return platform.WriteFile(path, data, 0o600)
	}, progress)
	digest := reader.Sum()
	if err != nil {
		return vm.SleepArtifact{}, err
	}
	return vm.SleepArtifact{SizeBytes: sizeBytes, StoredSizeBytes: storedBytes, SHA256: hex.EncodeToString(digest), Parts: parts}, nil
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
func (store *SnapshotStore) DownloadSleepMemory(ctx context.Context, directory, source string, artifact vm.SleepArtifact) error {
	destination := &temporaryFileDestination{directory: directory}
	defer destination.discard()
	if err := download(ctx, store.httpClient, source, artifact.SHA256, destination, store.logger); err != nil {
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
func (store *SnapshotStore) RestoreSleepDisk(ctx context.Context, identifier, source string, artifact vm.SleepArtifact) error {
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
	if err := download(ctx, store.httpClient, source, artifact.SHA256, volume, store.logger); err != nil {
		return err
	}
	return platform.Run(ctx, "zfs", "rename", staging, dataset)
}
