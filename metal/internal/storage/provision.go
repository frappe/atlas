package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Prepare provisions the VM's kernel + rootfs and returns the boot config.
func (z *ZFS) Prepare(ctx context.Context, req Request) (BootConfig, error) {
	if err := os.MkdirAll(req.ChrootRoot, 0o755); err != nil {
		return BootConfig{}, err
	}

	kdir := filepath.Join(z.kernelDir, req.Ref)
	if err := link(filepath.Join(kdir, "vmlinux"), filepath.Join(req.ChrootRoot, "vmlinux")); err != nil {
		return BootConfig{}, err
	}

	// zfs clone <base>@ready <vm>: create the VM's writable zvol from the base
	// snapshot; copy-on-write, so it shares the base's blocks until written.
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
	// zfs set volsize=<N>M <vm>: change the zvol's provisioned block-device
	// capacity (M = MiB). Only ever grown here; the guest resizes its own fs.
	return run(ctx, "zfs", "set", fmt.Sprintf("volsize=%dM", diskMiB), z.vmDataset(vmID))
}

// Resize grows the VM disk to diskMiB (no-op if not already smaller). It never
// shrinks; the caller rejects a smaller request before reaching here.
func (z *ZFS) Resize(ctx context.Context, vmID string, diskMiB int) error {
	return z.grow(ctx, vmID, diskMiB)
}

// Release destroys the VM's disk and every snapshot under it. Idempotent: an
// already-gone dataset is not an error.
func (z *ZFS) Release(ctx context.Context, vmID string) error {
	// zfs destroy -r <vm>: destroy the zvol; -r (recursive) also destroys every
	// snapshot taken under it.
	if err := run(ctx, "zfs", "destroy", "-r", z.vmDataset(vmID)); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}
	return nil
}

// volsizeBytes reads a zvol's provisioned size in bytes.
func volsizeBytes(ctx context.Context, dataset string) (int64, error) {
	// zfs get -Hp -o value volsize <dataset>: the zvol's provisioned size in
	// exact bytes (-H no header, -p exact, -o value = just the number).
	out, err := output(ctx, "zfs", "get", "-Hp", "-o", "value", "volsize", dataset)
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(strings.TrimSpace(out), 10, 64)
}
