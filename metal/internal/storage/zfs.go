package storage

import "strings"

// ZFS resolves images from a ZFS pool. Each image ref has a read-only base zvol
// images/<ref> with a @ready snapshot; each VM's rootfs disk vms/<id> is a clone
// of it, optionally grown to a requested size, exposed as a block device node
// inside its chroot and owned by the VM's uid/gid. The kernel is a file under
// kernelDir/<ref>, hard-linked into the chroot.
//
// The implementation is split by concern:
//   - provision.go — Prepare/grow/Release, the per-VM disk lifecycle
//   - snapshot.go  — Snapshot/Snapshots/DeleteSnapshot/Restore/Usage
//   - chroot.go    — materialize the kernel + rootfs node into the jailer chroot
type ZFS struct {
	pool      string
	kernelDir string
}

func NewZFS(pool, kernelDir string) *ZFS {
	return &ZFS{pool: pool, kernelDir: kernelDir}
}

// Dataset and snapshot name helpers. A ref's base lives at <pool>/images/<ref>
// with a @ready snapshot. Each VM's disk is <pool>/vms/<id>, exposed at
// /dev/zvol/.... The VM ID and its rootfs disk use the same ID.
func (z *ZFS) baseDataset(ref string) string  { return z.pool + "/images/" + ref }
func (z *ZFS) baseSnapshot(ref string) string { return z.baseDataset(ref) + "@ready" }
func (z *ZFS) vmDataset(vmID string) string   { return z.pool + "/vms/" + vmID }
func (z *ZFS) devPath(vmID string) string     { return "/dev/zvol/" + z.vmDataset(vmID) }
func (z *ZFS) snap(vmID, name string) string  { return z.vmDataset(vmID) + "@" + name }

// notFoundAware maps a ZFS "does not exist" failure to ErrNotFound.
func notFoundAware(err error) error {
	if err != nil && strings.Contains(err.Error(), "does not exist") {
		return ErrNotFound
	}
	return err
}

var _ Resolver = (*ZFS)(nil)
