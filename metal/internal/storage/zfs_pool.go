package storage

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// bytesToMiBShift converts bytes to MiB.
const bytesToMiBShift = 20

// readySnapshotSuffix marks the snapshot a clone boots from.
const readySnapshotSuffix = "@ready"

// Capacity describes total and available pool space in MiB.
type Capacity struct {
	TotalMiB     uint64
	AvailableMiB uint64
}

// Capacity returns the storage pool capacity. The -Hp flags ask zpool for exact
// byte values in a script-readable form.
func (pool *ZFSPool) Capacity(ctx context.Context) (Capacity, error) {
	output, err := platform.Output(ctx, "zpool", "list", "-Hp", "-o", "size,free", pool.name)
	if err != nil {
		return Capacity{}, fmt.Errorf("read storage pool capacity: %w", err)
	}

	fields := strings.Fields(output)
	if len(fields) != 2 {
		return Capacity{}, fmt.Errorf("storage pool returned invalid capacity data")
	}

	total, err := strconv.ParseUint(fields[0], 10, 64)
	if err != nil {
		return Capacity{}, fmt.Errorf("parse storage pool size: %w", err)
	}
	available, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return Capacity{}, fmt.Errorf("parse storage pool free space: %w", err)
	}

	return Capacity{TotalMiB: total >> bytesToMiBShift, AvailableMiB: available >> bytesToMiBShift}, nil
}

// imagesDataset is the parent of every base image.
func (pool *ZFSPool) imagesDataset() string { return pool.name + "/images" }

// baseDataset holds the unpacked root file system of one image.
func (pool *ZFSPool) baseDataset(imageReference string) string {
	return pool.imagesDataset() + "/" + imageReference
}

// baseSnapshot is the point every VM disk clones from.
func (pool *ZFSPool) baseSnapshot(imageReference string) string {
	return pool.baseDataset(imageReference) + readySnapshotSuffix
}

// virtualMachineDataset holds the disk of one virtual machine.
func (pool *ZFSPool) virtualMachineDataset(virtualMachineID string) string {
	return pool.name + "/vms/" + virtualMachineID
}

// virtualMachineDevicePath is the block device of one virtual machine disk.
func (pool *ZFSPool) virtualMachineDevicePath(virtualMachineID string) string {
	return "/dev/zvol/" + pool.virtualMachineDataset(virtualMachineID)
}

// snapshot names one point-in-time copy of a virtual machine disk.
func (pool *ZFSPool) snapshot(virtualMachineID, snapshotName string) string {
	return pool.virtualMachineDataset(virtualMachineID) + "@" + snapshotName
}

// stagingDataset holds the read-only clone an upload reads from.
func (pool *ZFSPool) stagingDataset(snapshotID string) string {
	return pool.name + "/staging/" + snapshotID
}

// stagingDevicePath is the block device of one staging clone.
func (pool *ZFSPool) stagingDevicePath(snapshotID string) string {
	return "/dev/zvol/" + pool.stagingDataset(snapshotID)
}
