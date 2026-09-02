// Package firecracker implements vm.VMDriver on Firecracker. Each VM is a
// jailer'd firecracker process run as a systemd template unit; metald is only a
// client, talking to systemd (D-Bus) and each VM's API socket.
package firecracker

import (
	"context"
	"log"
	"os"
	"path/filepath"
	"sync"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

type Driver struct {
	cfg    Config
	units  systemd.Manager
	images storage.Resolver
	net    network.Allocator
	mu     sync.Mutex // guards id allocation
}

func New(cfg Config, units systemd.Manager, images storage.Resolver, net network.Allocator) *Driver {
	return &Driver{cfg: cfg, units: units, images: images, net: net}
}

func (d *Driver) Type() vm.DriverType { return vm.DriverFirecracker }

// Create allocates the VM's ids/network, starts the jailer'd firecracker unit,
// and does all pre-boot configuration. The guest is not booted (StateCreated);
// call Start for that.
func (d *Driver) Create(ctx context.Context, spec vm.Spec) (_ vm.VM, err error) {
	id := newID()

	uid, err := d.allocate(id, spec)
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			d.cleanup(context.WithoutCancel(ctx), id)
		}
	}()
	vc := vmConfig{ID: id, UID: uid, GID: uid, Sock: d.cfg.sockPath(id), Spec: spec}

	nic, err := d.net.Allocate(ctx, network.Request{VMID: id, Ref: spec.Network.Name, UID: uid, GID: uid})
	if err != nil {
		return nil, err
	}
	vc.IP, vc.MAC = nic.GuestIP, nic.MAC
	if err = d.cfg.writeVMConfig(vc); err != nil {
		return nil, err
	}

	// A warm image is loaded on the first Start, not cold-booted here. Mark it and
	// skip the cold pre-boot; Start does the load.
	if _, _, warm := d.images.ImageMemory(spec.Image.Name); warm {
		if err = d.cfg.writeWarmMark(id, spec.Image.Name); err != nil {
			return nil, err
		}
		return d.newMachine(vc), nil
	}
	if err = d.bootPrep(ctx, vc, nic); err != nil {
		return nil, err
	}
	return d.newMachine(vc), nil
}

// bootPrep (re)starts the VM's jailer unit and does all pre-boot configuration,
// leaving the guest ready for InstanceStart. Shared by Create and relaunch.
func (d *Driver) bootPrep(ctx context.Context, vc vmConfig, nic network.NIC) error {
	if err := d.cfg.writeJailerEnv(vc.ID, d.cfg.jailerArgs(vc.ID, vc.UID, vc.GID, nic.NetnsPath)); err != nil {
		return err
	}
	if err := d.units.Start(ctx, vc.ID); err != nil {
		return err
	}
	if err := d.units.SetLimits(ctx, vc.ID, limits(vc.Spec)); err != nil {
		return err
	}
	if err := waitSocket(ctx, vc.Sock); err != nil {
		return err
	}
	boot, err := d.images.Prepare(ctx, storage.Request{
		VMID: vc.ID, Ref: vc.Spec.Image.Name, ChrootRoot: d.cfg.chrootRoot(vc.ID),
		UID: vc.UID, GID: vc.GID, DiskMiB: vc.Spec.DiskMiB,
	})
	if err != nil {
		return err
	}
	log.Printf("firecracker: vm %s kernel=%s cmdline=%q", vc.ID, boot.Kernel, bootArgs(boot, nic))
	return configure(ctx, api.New(vc.Sock), vc.Spec, boot, nic)
}

// relaunch brings a stopped VM back up: it clears the old unit and jailer chroot,
// then re-runs bootPrep against the persisted disk and still-present netns,
// leaving the guest ready for InstanceStart.
func (d *Driver) relaunch(ctx context.Context, vc vmConfig) error {
	_ = d.units.Stop(ctx, vc.ID)                            // clear any failed/leftover unit state
	_ = os.RemoveAll(filepath.Dir(d.cfg.chrootRoot(vc.ID))) // jailer will not reuse an existing chroot
	return d.bootPrep(ctx, vc, d.net.Resolve(vc.ID))
}

// loadLaunch starts a fresh jailer unit for vc and resumes it from a memory
// snapshot (the state and mem files). It is shared by warm restore and warm
// create. When mmds is non-nil it refreshes the metadata service (new ssh keys and
// a generation token) after the load and before the resume, so a clone's guest can
// re-key and re-sync.
func (d *Driver) loadLaunch(ctx context.Context, vc vmConfig, stateFile, memFile string, mmds map[string]any) error {
	_ = d.units.Stop(ctx, vc.ID)
	_ = os.RemoveAll(filepath.Dir(d.cfg.chrootRoot(vc.ID))) // jailer will not reuse a chroot
	nic := d.net.Resolve(vc.ID)
	if err := d.cfg.writeJailerEnv(vc.ID, d.cfg.jailerArgs(vc.ID, vc.UID, vc.GID, nic.NetnsPath)); err != nil {
		return err
	}
	if err := d.units.Start(ctx, vc.ID); err != nil {
		return err
	}
	if err := d.units.SetLimits(ctx, vc.ID, limits(vc.Spec)); err != nil {
		return err
	}
	if err := waitSocket(ctx, vc.Sock); err != nil {
		return err
	}
	// Recreate the rootfs block node at /rootfs.img, the path the snapshot expects.
	// The kernel is inside the memory snapshot, so it is not needed here.
	if err := d.images.PrepareRootfs(ctx, storage.Request{
		VMID: vc.ID, Ref: vc.Spec.Image.Name, ChrootRoot: d.cfg.chrootRoot(vc.ID),
		UID: vc.UID, GID: vc.GID, DiskMiB: vc.Spec.DiskMiB,
	}); err != nil {
		return err
	}
	// Stage the snapshot files into a uid-owned dir in the chroot: copy the small
	// state file, hard-link the large read-only mem file.
	stage := filepath.Join(d.cfg.chrootRoot(vc.ID), "snap")
	if err := mkdirChown(stage, vc.UID, vc.GID); err != nil {
		return err
	}
	if err := copyChown(stateFile, filepath.Join(stage, "state"), vc.UID, vc.GID); err != nil {
		return err
	}
	if err := storage.LinkOrReflink(ctx, memFile, filepath.Join(stage, "mem")); err != nil {
		return err
	}
	cli := api.New(vc.Sock)
	if err := cli.LoadSnapshot(ctx, api.LoadSnapshotReq{
		SnapshotPath: "snap/state",
		MemBackend:   api.MemBackend{BackendPath: "snap/mem", BackendType: "File"},
		ResumeVM:     false, // load paused so a metadata refresh lands before the guest runs
	}); err != nil {
		return err
	}
	if mmds != nil {
		if err := cli.PutMmds(ctx, mmds); err != nil {
			log.Printf("firecracker: vm %s mmds refresh: %v", vc.ID, err)
		}
	}
	return cli.Resume(ctx)
}

// warmLaunch loads a VM from a warm image's memory and hands the guest a fresh
// metadata payload so it can re-key and re-sync as a clone.
func (d *Driver) warmLaunch(ctx context.Context, vc vmConfig, ref string) error {
	state, mem, warm := d.images.ImageMemory(ref)
	if !warm {
		return vm.ErrNotFound
	}
	var mmds map[string]any
	if len(vc.Spec.SSHKeys) > 0 {
		mmds = refreshMMDS(vc.ID, vc.Spec.SSHKeys)
	}
	return d.loadLaunch(ctx, vc, state, mem, mmds)
}

// Images lists the images VMs can be created from.
func (d *Driver) Images(ctx context.Context) ([]vm.Image, error) {
	imgs, err := d.images.Images(ctx)
	if err != nil {
		return nil, storageErr(err)
	}
	out := make([]vm.Image, len(imgs))
	for i, im := range imgs {
		out[i] = vm.Image{Ref: im.Ref, Warm: im.Warm, SizeMiB: im.SizeMiB, CreatedAt: im.CreatedAt}
	}
	return out, nil
}

// DeleteImage removes an image. ErrConflict if VMs cloned from it still exist.
func (d *Driver) DeleteImage(ctx context.Context, ref string) error {
	return storageErr(d.images.DeleteImage(ctx, ref))
}

// allocate reserves a uid/gid and persists an initial config so a concurrent
// Create sees the id as used.
func (d *Driver) allocate(id string, spec vm.Spec) (uint32, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	used, err := d.cfg.usedIDs()
	if err != nil {
		return 0, err
	}
	uid, err := d.cfg.IDs.Allocate(used)
	if err != nil {
		return 0, err
	}
	return uid, d.cfg.writeVMConfig(vmConfig{ID: id, UID: uid, GID: uid, Sock: d.cfg.sockPath(id), Spec: spec})
}

// cleanup best-effort releases everything Create allocated for id.
func (d *Driver) cleanup(ctx context.Context, id string) {
	_ = d.units.Stop(ctx, id)
	_ = d.net.Release(ctx, id)
	_ = d.images.Release(ctx, id)
	_ = os.RemoveAll(d.cfg.vmDir(id))
}

// Load reconstructs a VM handle from its persisted config, so it survives a
// metald restart. Returns vm.ErrNotFound if no such VM exists.
func (d *Driver) Load(ctx context.Context, id string) (vm.VM, error) {
	vc, err := d.cfg.readVMConfig(id)
	if err != nil {
		return nil, err
	}
	return d.newMachine(vc), nil
}

// List reconstructs handles for every VM with persisted state.
func (d *Driver) List(ctx context.Context) ([]vm.VM, error) {
	ids, err := d.cfg.listVMIDs()
	if err != nil {
		return nil, err
	}
	vms := make([]vm.VM, 0, len(ids))
	for _, id := range ids {
		vc, err := d.cfg.readVMConfig(id)
		if err != nil {
			continue // skip half-written dirs
		}
		vms = append(vms, d.newMachine(vc))
	}
	return vms, nil
}

var _ vm.VMDriver = (*Driver)(nil)
