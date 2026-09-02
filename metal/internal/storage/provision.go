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

// Prepare provisions the VM's kernel + rootfs and returns the boot config.
func (z *ZFS) Prepare(ctx context.Context, req Request) (BootConfig, error) {
	if err := os.MkdirAll(req.ChrootRoot, 0o755); err != nil {
		return BootConfig{}, err
	}

	kdir := filepath.Join(z.kernelDir, req.Ref)
	if err := link(filepath.Join(kdir, "vmlinux"), filepath.Join(req.ChrootRoot, "vmlinux")); err != nil {
		return BootConfig{}, err
	}

	// The disk persists across a stop, so a restart reuses it: only clone when it
	// is absent. rollback destroys the disk only if this call created it, so a
	// restart never discards existing data on a later error.
	created := false
	if !datasetExists(ctx, z.vmDataset(req.VMID)) {
		// zfs clone <base>@ready <vm>: create the VM's writable zvol from the base
		// snapshot; copy-on-write, so it shares the base's blocks until written.
		if err := hostcmd.Run(ctx, "zfs", "clone", z.baseSnapshot(req.Ref), z.vmDataset(req.VMID)); err != nil {
			return BootConfig{}, err
		}
		created = true
	}
	rollback := func() {
		if created {
			_ = z.Release(ctx, req.VMID)
		}
	}
	if err := z.grow(ctx, req.VMID, req.DiskMiB); err != nil {
		rollback()
		return BootConfig{}, err
	}

	node := filepath.Join(req.ChrootRoot, "rootfs.img")
	if err := mknodBlock(z.devPath(req.VMID), node, req.UID, req.GID); err != nil {
		rollback()
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
	return hostcmd.Run(ctx, "zfs", "set", fmt.Sprintf("volsize=%dM", diskMiB), z.vmDataset(vmID))
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
	if err := hostcmd.Run(ctx, "zfs", "destroy", "-r", z.vmDataset(vmID)); err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil
		}
		return err
	}
	return nil
}

// datasetExists reports whether a dataset (here, a VM's zvol) is present.
func datasetExists(ctx context.Context, name string) bool {
	// zfs list <dataset>: exits 0 if it exists, non-zero otherwise.
	return hostcmd.Run(ctx, "zfs", "list", name) == nil
}

// volsizeBytes reads a zvol's provisioned size in bytes.
func volsizeBytes(ctx context.Context, dataset string) (int64, error) {
	// zfs get -Hp -o value volsize <dataset>: the zvol's provisioned size in
	// exact bytes (-H no header, -p exact, -o value = just the number).
	out, err := hostcmd.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "volsize", dataset)
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(strings.TrimSpace(out), 10, 64)
}
