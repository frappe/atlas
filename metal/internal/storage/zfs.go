package storage

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const defaultBootArgs = "console=ttyS0 reboot=k panic=1 pci=off"

// ZFS resolves images from a ZFS pool. Each image ref has a read-only base zvol
// base/<ref> with a @ready snapshot; each VM's rootfs disk vms/<id> is a clone
// of it, optionally grown to a requested size, exposed as a block device node
// inside its chroot and owned by the VM's uid/gid. The kernel is a file under
// kernelDir/<ref>, hard-linked into the chroot.
type ZFS struct {
	pool      string
	kernelDir string
}

func NewZFS(pool, kernelDir string) *ZFS {
	return &ZFS{pool: pool, kernelDir: kernelDir}
}

func (z *ZFS) baseDataset(ref string) string  { return z.pool + "/base/" + ref }
func (z *ZFS) baseSnapshot(ref string) string { return z.baseDataset(ref) + "@ready" }
func (z *ZFS) vmDataset(vmID string) string   { return z.pool + "/vms/" + vmID }
func (z *ZFS) devPath(vmID string) string     { return "/dev/zvol/" + z.vmDataset(vmID) }

func (z *ZFS) Prepare(ctx context.Context, req Request) (BootConfig, error) {
	if err := os.MkdirAll(req.ChrootRoot, 0o755); err != nil {
		return BootConfig{}, err
	}

	kdir := filepath.Join(z.kernelDir, req.Ref)
	if err := link(filepath.Join(kdir, "vmlinux"), filepath.Join(req.ChrootRoot, "vmlinux")); err != nil {
		return BootConfig{}, err
	}

	if err := run(ctx, "zfs", "clone", z.baseSnapshot(req.Ref), z.vmDataset(req.VMID)); err != nil {
		return BootConfig{}, err
	}
	if err := z.grow(ctx, req.VMID, req.DiskMiB); err != nil {
		_ = z.Release(ctx, req.VMID)
		return BootConfig{}, err
	}

	node := filepath.Join(req.ChrootRoot, "rootfs.img")
	if err := mknodBlock(z.devPath(req.VMID), node, req.UID, req.GID); err != nil {
		_ = z.Release(ctx, req.VMID)
		return BootConfig{}, err
	}

	return BootConfig{
		Kernel:     "/vmlinux",
		KernelArgs: bootArgs(kdir),
		Drives:     []Drive{{Path: "/rootfs.img", ReadOnly: false, Root: true}},
	}, nil
}

// grow extends the disk to diskMiB when that is larger than the current size.
// The guest must grow its filesystem to use the extra space.
func (z *ZFS) grow(ctx context.Context, vmID string, diskMiB int) error {
	if diskMiB <= 0 {
		return nil
	}
	want := int64(diskMiB) << 20
	cur, err := volsizeBytes(ctx, z.vmDataset(vmID))
	if err != nil {
		return err
	}
	if want <= cur {
		return nil
	}
	return run(ctx, "zfs", "set", fmt.Sprintf("volsize=%dM", diskMiB), z.vmDataset(vmID))
}

// Release destroys the VM's disk and every snapshot under it. Idempotent: an
// already-gone dataset is not an error.
func (z *ZFS) Release(ctx context.Context, vmID string) error {
	if err := run(ctx, "zfs", "destroy", "-r", z.vmDataset(vmID)); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}
	return nil
}

func volsizeBytes(ctx context.Context, dataset string) (int64, error) {
	out, err := exec.CommandContext(ctx, "zfs", "get", "-Hp", "-o", "value", "volsize", dataset).Output()
	if err != nil {
		return 0, fmt.Errorf("zfs get volsize %s: %w", dataset, err)
	}
	return strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
}

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

func link(src, dst string) error {
	_ = os.Remove(dst)
	return os.Link(src, dst)
}

func bootArgs(imageDir string) string {
	b, err := os.ReadFile(filepath.Join(imageDir, "boot-args"))
	if err != nil {
		return defaultBootArgs
	}
	return strings.TrimSpace(string(b))
}

func run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

var _ Resolver = (*ZFS)(nil)
