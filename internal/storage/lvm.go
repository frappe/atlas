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

// LVM resolves images from a thin LVM pool. Each image ref has a read-only base
// thin LV base-<ref>; each VM's rootfs disk vm-<id> is a thin snapshot of it,
// optionally grown to a requested size, exposed as a block device node inside
// its chroot and owned by the VM's uid/gid. The kernel is a file under
// kernelDir/<ref>, hard-linked into the chroot.
type LVM struct {
	vg        string
	kernelDir string
}

func NewLVM(vg, kernelDir string) *LVM {
	return &LVM{vg: vg, kernelDir: kernelDir}
}

func diskName(vmID string) string { return "vm-" + vmID }
func baseName(ref string) string  { return "base-" + ref }

func (l *LVM) lv(name string) string      { return l.vg + "/" + name }
func (l *LVM) devPath(name string) string { return "/dev/" + l.vg + "/" + name }

func (l *LVM) Prepare(ctx context.Context, req Request) (BootConfig, error) {
	if err := os.MkdirAll(req.ChrootRoot, 0o755); err != nil {
		return BootConfig{}, err
	}

	kdir := filepath.Join(l.kernelDir, req.Ref)
	if err := link(filepath.Join(kdir, "vmlinux"), filepath.Join(req.ChrootRoot, "vmlinux")); err != nil {
		return BootConfig{}, err
	}

	disk := diskName(req.VMID)
	if err := run(ctx, "lvcreate", "-s", "-kn", "--name", disk, l.lv(baseName(req.Ref))); err != nil {
		return BootConfig{}, err
	}
	if err := l.grow(ctx, disk, req.DiskMiB); err != nil {
		_ = l.Release(ctx, req.VMID)
		return BootConfig{}, err
	}

	node := filepath.Join(req.ChrootRoot, "rootfs.img")
	if err := mknodBlock(l.devPath(disk), node, req.UID, req.GID); err != nil {
		_ = l.Release(ctx, req.VMID)
		return BootConfig{}, err
	}

	return BootConfig{
		Kernel:     "/vmlinux",
		KernelArgs: bootArgs(kdir),
		Drives:     []Drive{{Path: "/rootfs.img", ReadOnly: false, Root: true}},
	}, nil
}

// grow extends the disk to diskMiB when that is larger than the base size.
// The guest must grow its filesystem to use the extra space.
func (l *LVM) grow(ctx context.Context, disk string, diskMiB int) error {
	if diskMiB <= 0 {
		return nil
	}
	want := int64(diskMiB) << 20
	cur, err := lvBytes(ctx, l.lv(disk))
	if err != nil {
		return err
	}
	if want <= cur {
		return nil
	}
	return run(ctx, "lvextend", "-L", fmt.Sprintf("%dM", diskMiB), l.lv(disk))
}

func (l *LVM) Release(ctx context.Context, vmID string) error {
	return run(ctx, "lvremove", "--force", l.lv(diskName(vmID)))
}

func lvBytes(ctx context.Context, vglv string) (int64, error) {
	out, err := exec.CommandContext(ctx, "lvs", "--noheadings", "--nosuffix", "--units", "b", "-o", "lv_size", vglv).Output()
	if err != nil {
		return 0, fmt.Errorf("lvs %s: %w", vglv, err)
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

// statBlock waits briefly for a freshly created device node to appear (udev lag).
func statBlock(path string) (syscall.Stat_t, error) {
	var st syscall.Stat_t
	for range 20 {
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

var _ Resolver = (*LVM)(nil)
