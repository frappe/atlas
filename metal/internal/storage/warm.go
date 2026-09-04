package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
	"github.com/frappe/atlas/metal/internal/vm"
)

// WarmImageArtifacts contains host-local Firecracker snapshot files.
type WarmImageArtifacts struct {
	Key          string
	RootSnapshot string
	StateFile    string
	MemoryFile   string
}

// WarmImagePromotion identifies a local template snapshot.
type WarmImagePromotion struct {
	Image                    vm.ImageRef
	Configuration            vm.MemorySnapshotConfiguration
	FirecrackerCompatibility string
	SourceVirtualMachineID   string
	SourceSnapshotName       string
	StateFile                string
	MemoryFile               string
}

// WarmImageKey identifies an exact image, shape, and Firecracker build.
func WarmImageKey(image vm.ImageRef, configuration vm.MemorySnapshotConfiguration, firecrackerCompatibility string) string {
	identity := struct {
		Reference                string
		Architecture             string
		RootfsSHA256             string
		KernelSHA256             string
		Configuration            vm.MemorySnapshotConfiguration
		FirecrackerCompatibility string
	}{
		Reference:                image.Name,
		Architecture:             image.Architecture,
		RootfsSHA256:             strings.ToLower(image.RootfsSHA256),
		KernelSHA256:             strings.ToLower(image.KernelSHA256),
		Configuration:            configuration,
		FirecrackerCompatibility: firecrackerCompatibility,
	}
	data, _ := json.Marshal(identity)
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

// WarmImage returns compatible local warm artifacts when all files exist.
func (store *ImageStore) WarmImage(ctx context.Context, image vm.ImageRef, configuration vm.MemorySnapshotConfiguration, firecrackerCompatibility string) (WarmImageArtifacts, bool, error) {
	key := WarmImageKey(image, configuration, firecrackerCompatibility)
	artifacts := store.warmImageArtifacts(image.Name, key)

	exists, err := datasetExists(ctx, strings.TrimSuffix(artifacts.RootSnapshot, "@ready"))
	if err != nil || !exists {
		return WarmImageArtifacts{}, false, err
	}
	for _, path := range []string{artifacts.StateFile, artifacts.MemoryFile} {
		if _, err := os.Stat(path); err != nil {
			if os.IsNotExist(err) {
				return WarmImageArtifacts{}, false, nil
			}
			return WarmImageArtifacts{}, false, err
		}
	}
	return artifacts, true, nil
}

// CreateWarmSourceSnapshot captures a template disk at one point in time.
func (store *ImageStore) CreateWarmSourceSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error {
	return hostcmd.Run(ctx, "zfs", "snapshot", store.pool.snapshot(virtualMachineID, snapshotName))
}

// DeleteWarmSourceSnapshot removes a template disk snapshot.
func (store *ImageStore) DeleteWarmSourceSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error {
	return destroyIfPresent(ctx, store.pool.snapshot(virtualMachineID, snapshotName))
}

// PromoteWarmSnapshot saves local root disk, state, and memory artifacts.
func (store *ImageStore) PromoteWarmSnapshot(ctx context.Context, promotion WarmImagePromotion) (WarmImageArtifacts, error) {
	key := WarmImageKey(promotion.Image, promotion.Configuration, promotion.FirecrackerCompatibility)
	artifacts := store.warmImageArtifacts(promotion.Image.Name, key)
	if _, found, err := store.WarmImage(ctx, promotion.Image, promotion.Configuration, promotion.FirecrackerCompatibility); err != nil || found {
		return artifacts, err
	}

	lock := store.imageLock(promotion.Image.Name)
	lock.Lock()
	defer lock.Unlock()

	destinationDataset := strings.TrimSuffix(artifacts.RootSnapshot, "@ready")
	complete := false
	defer func() {
		if !complete {
			_ = destroyIfPresent(context.Background(), destinationDataset)
			_ = os.RemoveAll(filepath.Dir(artifacts.StateFile))
		}
	}()

	sourceSnapshot := store.pool.snapshot(promotion.SourceVirtualMachineID, promotion.SourceSnapshotName)
	if err := sendSnapshot(ctx, sourceSnapshot, destinationDataset); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := hostcmd.Run(ctx, "zfs", "destroy", destinationDataset+"@"+promotion.SourceSnapshotName); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := hostcmd.Run(ctx, "zfs", "snapshot", artifacts.RootSnapshot); err != nil {
		return WarmImageArtifacts{}, err
	}

	directory := filepath.Dir(artifacts.StateFile)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := copyReflink(ctx, promotion.StateFile, artifacts.StateFile); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := copyReflink(ctx, promotion.MemoryFile, artifacts.MemoryFile); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := os.Chmod(artifacts.StateFile, 0o644); err != nil {
		return WarmImageArtifacts{}, err
	}
	if err := os.Chmod(artifacts.MemoryFile, 0o644); err != nil {
		return WarmImageArtifacts{}, err
	}

	complete = true
	return artifacts, nil
}

// RemoveOtherWarmImages removes artifacts that do not match the desired key.
func (store *ImageStore) RemoveOtherWarmImages(ctx context.Context, imageReference, desiredKey string) error {
	directory := filepath.Join(store.imageDirectory(imageReference), "warm")
	entries, err := os.ReadDir(directory)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !entry.IsDir() || entry.Name() == desiredKey {
			continue
		}
		if err := destroyIfPresent(ctx, store.warmDataset(entry.Name())); err != nil {
			return err
		}
		if err := os.RemoveAll(filepath.Join(directory, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

func (store *ImageStore) removeWarmImages(ctx context.Context, imageReference string) error {
	return store.RemoveOtherWarmImages(ctx, imageReference, "")
}

func (store *ImageStore) warmImageArtifacts(imageReference, key string) WarmImageArtifacts {
	directory := filepath.Join(store.imageDirectory(imageReference), "warm", key)
	return WarmImageArtifacts{
		Key:          key,
		RootSnapshot: store.warmDataset(key) + "@ready",
		StateFile:    filepath.Join(directory, "state"),
		MemoryFile:   filepath.Join(directory, "memory"),
	}
}

func (store *ImageStore) warmDataset(key string) string {
	return store.pool.name + "/warm/" + key
}

func copyReflink(ctx context.Context, source, destination string) error {
	return hostcmd.Run(ctx, "cp", "--reflink=auto", source, destination)
}

func sendSnapshot(ctx context.Context, sourceSnapshot, destinationDataset string) error {
	sendCommand := exec.CommandContext(ctx, "zfs", "send", sourceSnapshot)
	receiveCommand := exec.CommandContext(ctx, "zfs", "recv", destinationDataset)

	pipe, err := sendCommand.StdoutPipe()
	if err != nil {
		return err
	}
	receiveCommand.Stdin = pipe

	var sendError strings.Builder
	var receiveError strings.Builder
	sendCommand.Stderr = &sendError
	receiveCommand.Stderr = &receiveError
	if err := receiveCommand.Start(); err != nil {
		return err
	}
	if err := sendCommand.Run(); err != nil {
		_ = receiveCommand.Wait()
		return fmt.Errorf("zfs send: %w: %s", err, strings.TrimSpace(sendError.String()))
	}
	if err := receiveCommand.Wait(); err != nil {
		return fmt.Errorf("zfs receive: %w: %s", err, strings.TrimSpace(receiveError.String()))
	}
	return nil
}
