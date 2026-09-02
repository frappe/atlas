package firecracker

// Snapshot operations: pause/resume, disk-plus-memory snapshots, restore in place,
// and promote a memory snapshot to a warm image. The file-staging helpers here are
// shared with the driver's load path (driver.go).

import (
	"context"
	"io"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// status reports the VM's current state.
func (m *machine) status(ctx context.Context) (vm.State, error) {
	st, err := m.d.units.Status(ctx, m.cfg.ID)
	if err != nil {
		return "", err
	}
	return m.state(ctx, st), nil
}

// Pause halts the guest's vCPUs. Returns ErrConflict unless the VM is running.
func (m *machine) Pause(ctx context.Context) error {
	s, err := m.status(ctx)
	if err != nil {
		return err
	}
	if s != vm.StateRunning {
		return vm.ErrConflict
	}
	return m.api.Pause(ctx)
}

// Resume returns a paused guest to running. Returns ErrConflict otherwise.
func (m *machine) Resume(ctx context.Context) error {
	s, err := m.status(ctx)
	if err != nil {
		return err
	}
	if s != vm.StatePaused {
		return vm.ErrConflict
	}
	return m.api.Resume(ctx)
}

// Snapshot takes a named disk snapshot. When memory is true it also pauses the
// guest, captures RAM and device state paired with the disk snapshot, and resumes.
func (m *machine) Snapshot(ctx context.Context, name string, memory bool) error {
	if !memory {
		return storageErr(m.d.images.Snapshot(ctx, m.cfg.ID, name))
	}
	s, err := m.status(ctx)
	if err != nil {
		return err
	}
	if s != vm.StateRunning && s != vm.StatePaused {
		return vm.ErrConflict
	}
	if s == vm.StateRunning {
		if err := m.api.Pause(ctx); err != nil {
			return err
		}
		// Always resume, even if a later step fails, so a snapshot never leaves the
		// guest paused.
		defer func() { _ = m.api.Resume(context.WithoutCancel(ctx)) }()
	}
	// The guest is paused, so the disk is quiescent: take the paired disk snapshot.
	if err := m.d.images.Snapshot(ctx, m.cfg.ID, name); err != nil {
		return storageErr(err)
	}
	// Firecracker writes into a uid-owned dir inside the chroot; move the files out
	// to the persistent per-VM snapshot dir, which outlives the chroot.
	stage := filepath.Join(m.d.cfg.chrootRoot(m.cfg.ID), "snap")
	if err := mkdirChown(stage, m.cfg.UID, m.cfg.GID); err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	if err := m.api.CreateSnapshot(ctx, api.CreateSnapshotReq{
		SnapshotType: "Full", SnapshotPath: "snap/state", MemFilePath: "snap/mem",
	}); err != nil {
		return err
	}
	dir := m.d.cfg.snapDir(m.cfg.ID, name)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	state, mem := m.d.cfg.snapFiles(m.cfg.ID, name)
	if err := moveFile(filepath.Join(stage, "state"), state); err != nil {
		return err
	}
	if err := moveFile(filepath.Join(stage, "mem"), mem); err != nil {
		return err
	}
	// 0644 so any VM's uid can read the file when it is hard-linked into a chroot
	// on restore or warm start.
	_ = os.Chmod(state, 0o644)
	_ = os.Chmod(mem, 0o644)
	return nil
}

// Snapshots lists the VM's snapshots, marking those that carry memory.
func (m *machine) Snapshots(ctx context.Context) ([]vm.Snapshot, error) {
	disk, err := m.d.images.Snapshots(ctx, m.cfg.ID)
	if err != nil {
		return nil, storageErr(err)
	}
	out := make([]vm.Snapshot, len(disk))
	for i, s := range disk {
		out[i] = vm.Snapshot{
			Name:      s.Name,
			Memory:    m.d.cfg.hasSnapMemory(m.cfg.ID, s.Name),
			SizeMiB:   s.SizeMiB,
			UsedMiB:   s.UsedMiB,
			CreatedAt: s.CreatedAt,
		}
	}
	return out, nil
}

// DeleteSnapshot removes a snapshot's disk snapshot and its memory files, if any.
func (m *machine) DeleteSnapshot(ctx context.Context, name string) error {
	if err := m.d.images.DeleteSnapshot(ctx, m.cfg.ID, name); err != nil {
		return storageErr(err)
	}
	_ = os.RemoveAll(m.d.cfg.snapDir(m.cfg.ID, name))
	return nil
}

// RestoreSnapshot rolls the VM back to a snapshot. A memory snapshot reloads RAM
// so the VM resumes at the captured instant; a disk-only snapshot leaves the VM
// stopped to cold-boot from the rolled-back disk on the next Start.
func (m *machine) RestoreSnapshot(ctx context.Context, name string) error {
	warm := m.d.cfg.hasSnapMemory(m.cfg.ID, name)
	// A fresh firecracker process is required to load, and rollback needs the zvol
	// free, so stop the VM first if it is up. We roll the disk back next, so a
	// forced stop is fine.
	s, err := m.status(ctx)
	if err != nil {
		return err
	}
	if s == vm.StateRunning || s == vm.StatePaused {
		if err := m.Stop(ctx, true); err != nil {
			return err
		}
	}
	if err := m.d.images.Restore(ctx, m.cfg.ID, name); err != nil {
		return storageErr(err)
	}
	if !warm {
		return nil
	}
	state, mem := m.d.cfg.snapFiles(m.cfg.ID, name)
	return m.d.loadLaunch(ctx, m.cfg, state, mem, nil)
}

// Promote builds a standalone warm image from one of the VM's memory snapshots.
func (m *machine) Promote(ctx context.Context, name, imageRef string) error {
	if !m.d.cfg.hasSnapMemory(m.cfg.ID, name) {
		return vm.ErrConflict // promote needs a memory snapshot
	}
	state, mem := m.d.cfg.snapFiles(m.cfg.ID, name)
	return storageErr(m.d.images.Promote(ctx, storage.PromoteRequest{
		SrcVMID:   m.cfg.ID,
		SnapName:  name,
		SrcRef:    m.cfg.Spec.Image.Name,
		Ref:       imageRef,
		StateFile: state,
		MemFile:   mem,
	}))
}

// mkdirChown makes a directory owned by the VM's uid/gid, so the jailed
// firecracker can write into it.
func mkdirChown(path string, uid, gid uint32) error {
	if err := os.MkdirAll(path, 0o750); err != nil {
		return err
	}
	return os.Chown(path, int(uid), int(gid))
}

// copyChown copies src to dst and gives dst to the VM's uid/gid.
func copyChown(src, dst string, uid, gid uint32) error {
	if err := copyFile(src, dst); err != nil {
		return err
	}
	return os.Chown(dst, int(uid), int(gid))
}

// copyFile copies src to a fresh dst.
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o640)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return err
	}
	return out.Close()
}

// moveFile moves src to dst, preferring an instant rename and falling back to a
// copy across filesystems.
func moveFile(src, dst string) error {
	if err := os.Rename(src, dst); err == nil {
		return nil
	}
	if err := copyFile(src, dst); err != nil {
		return err
	}
	return os.Remove(src)
}
