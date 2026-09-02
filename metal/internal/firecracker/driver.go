// Package firecracker implements vm.VMDriver on Firecracker. Each VM is a
// jailer'd firecracker process run as a systemd template unit; metald is only a
// client, talking to systemd (D-Bus) and each VM's API socket.
package firecracker

import (
	"context"
	"errors"
	"log"
	"os"
	"path/filepath"
	"sync"

	"github.com/google/uuid"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

var errNotImplemented = errors.New("firecracker: not implemented")

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
	// A version 7 UUID sorts by creation time. metald dials a VM's API socket
	// through a short symlink, so the id length has no limit to respect.
	id := uuid.Must(uuid.NewV7()).String()

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
	if err := d.cfg.linkSocket(vc.ID); err != nil {
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
	_ = os.Remove(d.cfg.sockPath(id))
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
