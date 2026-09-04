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

// Release removes a virtual machine disk and its snapshots.
func (store *VirtualMachineStore) Release(ctx context.Context, virtualMachineID string) error {
	if err := hostcmd.Run(ctx, "zfs", "destroy", "-r", store.pool.virtualMachineDataset(virtualMachineID)); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}

	return nil
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
