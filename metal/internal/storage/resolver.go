// Package storage manages virtual machine images and disks.
package storage

import (
	"errors"

	"github.com/frappe/atlas/metal/internal/vm"
)

// ErrNotFound indicates that a disk, snapshot, or image does not exist.
var ErrNotFound = errors.New("storage: not found")

// ErrInUse indicates that an image has dependent virtual machines.
var ErrInUse = errors.New("storage: in use")

// ErrImageConflict indicates that an image reference has different content.
var ErrImageConflict = errors.New("storage: image content conflict")

// ErrImageIntegrity indicates that image verification failed.
var ErrImageIntegrity = errors.New("storage: image integrity check failed")

// VirtualMachineStorageRequest identifies the files and disk for one virtual machine.
type VirtualMachineStorageRequest struct {
	VirtualMachineID string
	ImageReference   string
	Image            vm.ImageRef
	ChrootRoot       string
	UserID           uint32
	GroupID          uint32
	DiskMiB          int
	SourceSnapshot   string
}

// BootConfiguration contains the files that Firecracker needs to boot.
type BootConfiguration struct {
	Kernel     string
	KernelArgs string
	Drives     []Drive
}

// Drive describes one Firecracker block device.
type Drive struct {
	Path     string
	ReadOnly bool
	Root     bool
}

// Usage describes disk allocation.
type Usage struct {
	SizeMiB int
	UsedMiB int
}
