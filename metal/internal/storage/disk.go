package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

const (
	// defaultKernelArguments boot a Firecracker guest on the serial console.
	defaultKernelArguments = "console=ttyS0 reboot=k panic=1 pci=off"

	// bootArgumentsFileName lets one image override the kernel arguments.
	bootArgumentsFileName = "boot-args"

	// blockDeviceAttempts and blockDeviceRetryDelay bound the wait for udev to
	// create a zvol device node, which does not appear when the dataset does.
	blockDeviceAttempts   = 60
	blockDeviceRetryDelay = 50 * time.Millisecond
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

// HasDisk reports whether the VM already has a disk.
func (store *VirtualMachineStore) HasDisk(ctx context.Context, virtualMachineID string) (bool, error) {
	return datasetExists(ctx, store.pool.virtualMachineDataset(virtualMachineID))
}

// provisionDisk clones the image, grows the disk, and exposes it in the chroot.
// A disk this call created is released when a later step fails, so a failed boot
// does not leave a half-prepared disk behind.
func (store *VirtualMachineStore) provisionDisk(ctx context.Context, request VirtualMachineStorageRequest) error {
	exists, err := datasetExists(ctx, store.pool.virtualMachineDataset(request.VirtualMachineID))
	if err != nil {
		return err
	}

	if exists && request.SourceSnapshot != "" {
		isUnchanged, err := isUnchangedClone(ctx, store.pool.virtualMachineDataset(request.VirtualMachineID), request.SourceSnapshot)
		if err != nil {
			return err
		}
		if !isUnchanged {
			return fmt.Errorf("prepare VM %s from %s: %w", request.VirtualMachineID, request.SourceSnapshot, ErrDiskNotFresh)
		}
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
		if err := platform.Run(
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

// releaseCreatedDisk removes a disk only when this call created it.
func (store *VirtualMachineStore) releaseCreatedDisk(ctx context.Context, virtualMachineID string, created bool) {
	if created {
		_ = store.Release(context.WithoutCancel(ctx), virtualMachineID)
	}
}

// growDisk raises the volume size. A disk never shrinks, because the guest file
// system inside it cannot be shrunk safely.
func (store *VirtualMachineStore) growDisk(ctx context.Context, virtualMachineID string, diskMiB int) error {
	if diskMiB <= 0 {
		return nil
	}

	requestedSizeBytes := int64(diskMiB) << bytesToMiBShift
	currentSizeBytes, err := volumeSizeBytes(ctx, store.pool.virtualMachineDataset(virtualMachineID))
	if err != nil {
		return err
	}
	if requestedSizeBytes <= currentSizeBytes {
		return nil
	}

	return platform.Run(
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

// Release removes the VM disk and keeps dependent staging clones.
func (store *VirtualMachineStore) Release(ctx context.Context, virtualMachineID string) error {
	dataset := store.pool.virtualMachineDataset(virtualMachineID)

	if err := store.promoteDependentClones(ctx, dataset); err != nil {
		return err
	}

	if err := platform.Run(ctx, "zfs", "destroy", "-r", dataset); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}

	return nil
}

// promoteDependentClones gives each staging clone ownership of the snapshot it
// reads. ZFS cannot destroy a snapshot while a clone of it exists, and a pending
// snapshot upload still needs its source after the VM disk goes away.
func (store *VirtualMachineStore) promoteDependentClones(ctx context.Context, dataset string) error {
	output, err := platform.Output(ctx,
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
		if err := platform.Run(ctx, "zfs", "promote", clone); err != nil {
			return fmt.Errorf("promote dependent clone %s: %w", clone, err)
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

// datasetExists reports whether one ZFS dataset is present.
func datasetExists(ctx context.Context, name string) (bool, error) {
	err := platform.Run(ctx, "zfs", "list", name)
	if err == nil {
		return true, nil
	}
	if strings.Contains(err.Error(), "does not exist") {
		return false, nil
	}

	return false, fmt.Errorf("check ZFS dataset %s: %w", name, err)
}

// isUnchangedClone reports whether a dataset is a clone of the snapshot with no
// writes since the clone.
func isUnchangedClone(ctx context.Context, dataset string, snapshot string) (bool, error) {
	output, err := platform.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "origin,written", dataset)
	if err != nil {
		return false, err
	}

	values := strings.Fields(output)
	if len(values) != 2 {
		return false, fmt.Errorf("read ZFS clone state of %s: unexpected output %q", dataset, output)
	}
	return values[0] == snapshot && values[1] == "0", nil
}

// volumeSizeBytes reads the provisioned size of one volume.
func volumeSizeBytes(ctx context.Context, dataset string) (int64, error) {
	output, err := platform.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "volsize", dataset)
	if err != nil {
		return 0, err
	}

	return strconv.ParseInt(strings.TrimSpace(output), 10, 64)
}

// DiskUsage reports disk size and allocated storage.
func (store *VirtualMachineStore) DiskUsage(ctx context.Context, virtualMachineID string) (Usage, error) {
	output, err := platform.Output(
		ctx,
		"zfs",
		"get",
		"-Hp",
		"-o",
		"property,value",
		"volsize,used",
		store.pool.virtualMachineDataset(virtualMachineID),
	)
	if err != nil {
		return Usage{}, notFoundAware(err)
	}
	return parseDiskUsage(output), nil
}

// parseDiskUsage reads volsize and used from `zfs get` property output.
func parseDiskUsage(output string) Usage {
	var usage Usage
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}

		valueBytes, _ := strconv.ParseInt(fields[1], 10, 64)
		switch fields[0] {
		case "volsize":
			usage.SizeMiB = int(valueBytes >> bytesToMiBShift)
		case "used":
			usage.UsedMiB = int(valueBytes >> bytesToMiBShift)
		}
	}
	return usage
}

// createBlockDevice makes the VM disk reachable inside the jailer chroot. The
// node is recreated from the zvol major and minor numbers, because the chroot
// cannot follow a symlink out to /dev.
func createBlockDevice(sourceDevice, destinationPath string, userID, groupID uint32) error {
	deviceInformation, err := waitForBlockDevice(sourceDevice)
	if err != nil {
		return err
	}

	_ = os.Remove(destinationPath)
	if err := syscall.Mknod(destinationPath, syscall.S_IFBLK|0o600, int(deviceInformation.Rdev)); err != nil {
		return fmt.Errorf("create block device %s: %w", destinationPath, err)
	}
	if err := os.Chmod(destinationPath, 0o600); err != nil {
		return err
	}

	return os.Chown(destinationPath, int(userID), int(groupID))
}

// waitForBlockDevice waits for udev to create a device node.
func waitForBlockDevice(path string) (syscall.Stat_t, error) {
	var deviceInformation syscall.Stat_t
	for range blockDeviceAttempts {
		if err := syscall.Stat(path, &deviceInformation); err == nil {
			return deviceInformation, nil
		}
		time.Sleep(blockDeviceRetryDelay)
	}

	return deviceInformation, fmt.Errorf("device %s did not appear", path)
}

// replaceHardLink relinks destination to source, replacing any earlier link.
func replaceHardLink(source, destination string) error {
	_ = os.Remove(destination)
	return os.Link(source, destination)
}

// LinkOrCopy shares data when possible and copies it when required. A hard link
// is used first, and a reflink copy covers a destination on another file system.
func LinkOrCopy(ctx context.Context, source, destination string) error {
	_ = os.Remove(destination)
	if err := os.Link(source, destination); err == nil {
		return nil
	}

	return platform.Run(ctx, "cp", "--reflink=auto", source, destination)
}

// kernelArguments returns the image override when present, or the defaults.
func kernelArguments(imageDirectory string) string {
	arguments, err := os.ReadFile(filepath.Join(imageDirectory, bootArgumentsFileName))
	if err != nil {
		return defaultKernelArguments
	}

	return strings.TrimSpace(string(arguments))
}
