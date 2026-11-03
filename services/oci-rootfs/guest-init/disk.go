package main

import (
	"os"
	"unsafe"

	"golang.org/x/sys/unix"
)

const (
	rootDevicePath = "/dev/vda"

	// ext4ResizeFS is EXT4_IOC_RESIZE_FS, which x/sys/unix does not define.
	ext4ResizeFS = 0x40086610
)

// grownSizeBytes is the device size of the last growth. Only the metadata watch loop uses it.
var grownSizeBytes uint64

// growRootFileSystem grows ext4 online to the device size, at boot and after a disk
// resize. The image may have no resize2fs.
func growRootFileSystem() error {
	device, err := os.Open(rootDevicePath)
	if err != nil {
		return err
	}
	defer device.Close()

	var sizeBytes uint64
	if _, _, errno := unix.Syscall(unix.SYS_IOCTL, device.Fd(), unix.BLKGETSIZE64, uintptr(unsafe.Pointer(&sizeBytes))); errno != 0 {
		return errno
	}
	if sizeBytes == grownSizeBytes {
		return nil
	}

	var statistics unix.Statfs_t
	if err := unix.Statfs("/", &statistics); err != nil {
		return err
	}
	root, err := os.Open("/")
	if err != nil {
		return err
	}
	defer root.Close()

	blocks := sizeBytes / uint64(statistics.Bsize)
	if _, _, errno := unix.Syscall(unix.SYS_IOCTL, root.Fd(), ext4ResizeFS, uintptr(unsafe.Pointer(&blocks))); errno != 0 {
		return errno
	}
	grownSizeBytes = sizeBytes
	return nil
}
