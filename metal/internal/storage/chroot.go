package storage

// Helpers that materialize a VM's boot files inside the jailer chroot: the
// hard-linked kernel, the rootfs block-device node, and the kernel cmdline.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const defaultBootArgs = "console=ttyS0 reboot=k panic=1 pci=off"

// mknodBlock creates a block-device node at dstPath mirroring srcDev, owned by
// the VM's uid/gid so the jailed firecracker can open it.
func mknodBlock(srcDev, dstPath string, uid, gid uint32) error {
	st, err := statBlock(srcDev)
	if err != nil {
		return err
	}
	_ = os.Remove(dstPath)
	if err := syscall.Mknod(dstPath, syscall.S_IFBLK|0o600, int(st.Rdev)); err != nil {
		return fmt.Errorf("mknod %s: %w", dstPath, err)
	}
	if err := os.Chmod(dstPath, 0o600); err != nil {
		return err
	}
	return os.Chown(dstPath, int(uid), int(gid))
}

// statBlock waits briefly for a freshly created device node to appear. A zvol's
// /dev/zvol symlink is created asynchronously by udev, so allow a few seconds.
func statBlock(path string) (syscall.Stat_t, error) {
	var st syscall.Stat_t
	for range 60 {
		if err := syscall.Stat(path, &st); err == nil {
			return st, nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	return st, fmt.Errorf("device %s did not appear", path)
}

// link hard-links src to dst, replacing any existing dst.
func link(src, dst string) error {
	_ = os.Remove(dst)
	return os.Link(src, dst)
}

// bootArgs reads the kernel cmdline from imageDir/boot-args, or the default.
func bootArgs(imageDir string) string {
	b, err := os.ReadFile(filepath.Join(imageDir, "boot-args"))
	if err != nil {
		return defaultBootArgs
	}
	return strings.TrimSpace(string(b))
}
