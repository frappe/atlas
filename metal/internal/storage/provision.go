package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// PrepareBoot prepares a disk and kernel for a cold boot.
func (store *VirtualMachineStore) PrepareBoot(ctx context.Context, request VirtualMachineStorageRequest) (BootConfiguration, error) {
	if err := os.MkdirAll(request.ChrootRoot, 0o755); err != nil {
		return BootConfiguration{}, err
	}
	if err := store.images.ensureImage(ctx, request.ImageReference, request.Image); err != nil {
		return BootConfiguration{}, err
	}
	if err := replaceHardLink(store.images.kernelFile(request.ImageReference), filepath.Join(request.ChrootRoot, "vmlinux")); err != nil {
		return BootConfiguration{}, err
	}
	if err := store.provisionDisk(ctx, request); err != nil {
		return BootConfiguration{}, err
	}

	return BootConfiguration{
		Kernel:     "/vmlinux",
		KernelArgs: kernelArguments(store.images.imageDirectory(request.ImageReference)),
		Drives:     []Drive{{Path: "/rootfs.img", Root: true}},
	}, nil
}

// PrepareRootFileSystem prepares a disk for snapshot restore.
func (store *VirtualMachineStore) PrepareRootFileSystem(ctx context.Context, request VirtualMachineStorageRequest) error {
	if err := os.MkdirAll(request.ChrootRoot, 0o755); err != nil {
		return err
	}

	return store.provisionDisk(ctx, request)
}

func (store *VirtualMachineStore) provisionDisk(ctx context.Context, request VirtualMachineStorageRequest) error {
	exists, err := datasetExists(ctx, store.pool.virtualMachineDataset(request.VirtualMachineID))
	if err != nil {
		return err
	}

	created := false
	if !exists {
		if err := store.images.ensureImage(ctx, request.ImageReference, request.Image); err != nil {
			return err
		}
		sourceSnapshot := request.SourceSnapshot
		if sourceSnapshot == "" {
			sourceSnapshot = store.pool.baseSnapshot(request.ImageReference)
		}
		if err := hostcmd.Run(
			ctx,
			"zfs",
			"clone",
			sourceSnapshot,
			store.pool.virtualMachineDataset(request.VirtualMachineID),
		); err != nil {
			return err
		}
		created = true
	}

	if err := store.growDisk(ctx, request.VirtualMachineID, request.DiskMiB); err != nil {
		store.releaseCreatedDisk(ctx, request.VirtualMachineID, created)
		return err
	}

	rootFileSystem := filepath.Join(request.ChrootRoot, "rootfs.img")
	if err := createBlockDevice(
		store.pool.virtualMachineDevicePath(request.VirtualMachineID),
		rootFileSystem,
		request.UserID,
		request.GroupID,
	); err != nil {
		store.releaseCreatedDisk(ctx, request.VirtualMachineID, created)
		return err
	}

	return nil
}

func (store *VirtualMachineStore) releaseCreatedDisk(ctx context.Context, virtualMachineID string, created bool) {
	if created {
		_ = store.Release(ctx, virtualMachineID)
	}
}

func (store *VirtualMachineStore) growDisk(ctx context.Context, virtualMachineID string, diskMiB int) error {
	if diskMiB <= 0 {
		return nil
	}

	requestedSizeBytes := int64(diskMiB) << 20
	currentSizeBytes, err := volumeSizeBytes(ctx, store.pool.virtualMachineDataset(virtualMachineID))
	if err != nil {
		return err
	}
	if requestedSizeBytes <= currentSizeBytes {
		return nil
	}

	return hostcmd.Run(
		ctx,
		"zfs",
		"set",
		fmt.Sprintf("volsize=%dM", diskMiB),
		store.pool.virtualMachineDataset(virtualMachineID),
	)
}

// ResizeDisk grows a virtual machine disk.
func (store *VirtualMachineStore) ResizeDisk(ctx context.Context, virtualMachineID string, diskMiB int) error {
	return store.growDisk(ctx, virtualMachineID, diskMiB)
}

// Release removes the VM disk and dependent staging clones.
func (store *VirtualMachineStore) Release(ctx context.Context, virtualMachineID string) error {
	dataset := store.pool.virtualMachineDataset(virtualMachineID)

	if err := store.destroyDependentClones(ctx, dataset); err != nil {
		return err
	}

	if err := hostcmd.Run(ctx, "zfs", "destroy", "-r", dataset); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}

	return nil
}

// destroyDependentClones removes the staging clones a snapshot upload leaves on
// the VM snapshots. ZFS cannot destroy a snapshot while a clone of it exists.
func (store *VirtualMachineStore) destroyDependentClones(ctx context.Context, dataset string) error {
	output, err := hostcmd.Output(ctx,
		"zfs", "get", "-Hp", "-r", "-t", "snapshot", "-o", "value", "clones", dataset)
	if err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}

	stagingPrefix := store.pool.name + "/staging/"
	for _, clone := range parseCloneList(output) {
		if !strings.HasPrefix(clone, stagingPrefix) {
			continue
		}
		if err := destroyIfPresent(ctx, clone); err != nil {
			return fmt.Errorf("destroy dependent clone %s: %w", clone, err)
		}
	}

	return nil
}

// parseCloneList reads clone values from `zfs get` output. One line per
// snapshot: "-" means no clones, and several clones are comma-separated.
func parseCloneList(output string) []string {
	var clones []string
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || line == "-" {
			continue
		}
		for _, clone := range strings.Split(line, ",") {
			if clone = strings.TrimSpace(clone); clone != "" {
				clones = append(clones, clone)
			}
		}
	}

	return clones
}

func datasetExists(ctx context.Context, name string) (bool, error) {
	err := hostcmd.Run(ctx, "zfs", "list", name)
	if err == nil {
		return true, nil
	}
	if strings.Contains(err.Error(), "does not exist") {
		return false, nil
	}

	return false, fmt.Errorf("check ZFS dataset %s: %w", name, err)
}

func volumeSizeBytes(ctx context.Context, dataset string) (int64, error) {
	output, err := hostcmd.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "volsize", dataset)
	if err != nil {
		return 0, err
	}

	return strconv.ParseInt(strings.TrimSpace(output), 10, 64)
}
