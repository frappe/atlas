// Package firecracker implements vm.VMDriver on Firecracker. Each VM is a
// jailer'd firecracker process run as a systemd template unit; metald is only a
// client, talking to systemd (D-Bus) and each VM's API socket.
package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"sync"
	"time"

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

	if err = d.cfg.writeJailerEnv(id, d.cfg.jailerArgs(id, uid, uid, nic.NetnsPath)); err != nil {
		return nil, err
	}
	if err = d.units.Start(ctx, id); err != nil {
		return nil, err
	}
	if err = d.units.SetLimits(ctx, id, limits(spec)); err != nil {
		return nil, err
	}

	if err = waitSocket(ctx, vc.Sock); err != nil {
		return nil, err
	}
	boot, err := d.images.Prepare(ctx, storage.Request{
		VMID: id, Ref: spec.Image.Name, ChrootRoot: d.cfg.chrootRoot(id),
		UID: uid, GID: uid, DiskMiB: spec.DiskMiB,
	})
	if err != nil {
		return nil, err
	}

	log.Printf("firecracker: vm %s kernel=%s cmdline=%q", id, boot.Kernel, bootArgs(boot, nic))
	if err = configure(ctx, api.New(vc.Sock), spec, boot, nic); err != nil {
		return nil, err
	}
	return d.newMachine(vc), nil
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

const (
	ifaceID     = "eth0"
	mmdsAddr    = "169.254.169.254"
	mmdsVersion = "V1" // simplest guest-side (plain GET); V2 adds token auth
)

// configure sends firecracker its pre-boot configuration over the API socket.
func configure(ctx context.Context, cli *api.Client, spec vm.Spec, boot storage.BootConfig, nic network.NIC) error {
	if err := cli.PutMachineConfig(ctx, api.MachineConfig{VCPUCount: spec.VCPUs, MemSizeMiB: spec.MemMiB}); err != nil {
		return err
	}
	if err := cli.PutBootSource(ctx, api.BootSource{KernelImagePath: boot.Kernel, BootArgs: bootArgs(boot, nic)}); err != nil {
		return err
	}
	for i, dr := range boot.Drives {
		if err := cli.PutDrive(ctx, api.Drive{
			DriveID: "drive" + strconv.Itoa(i), PathOnHost: dr.Path,
			IsRootDevice: dr.Root, IsReadOnly: dr.ReadOnly,
		}); err != nil {
			return err
		}
	}
	if err := cli.PutNetworkInterface(ctx, api.NetworkInterface{IfaceID: ifaceID, HostDevName: nic.TapName, GuestMAC: nic.MAC}); err != nil {
		return err
	}
	if len(spec.SSHKeys) > 0 {
		if err := cli.PutMmdsConfig(ctx, api.MmdsConfig{NetworkInterfaces: []string{ifaceID}, Version: mmdsVersion, IPv4Address: mmdsAddr}); err != nil {
			return err
		}
		if err := cli.PutMmds(ctx, mmdsData(spec.SSHKeys)); err != nil {
			return err
		}
	}
	return nil
}

// mmdsData builds an EC2-style metadata tree so cloud-init's Ec2 datasource
// finds the keys at /latest/meta-data/public-keys/<n>/openssh-key.
func mmdsData(keys []string) map[string]any {
	pk := make(map[string]any, len(keys))
	for i, k := range keys {
		pk[strconv.Itoa(i)] = map[string]any{"openssh-key": k}
	}
	return map[string]any{"latest": map[string]any{"meta-data": map[string]any{"public-keys": pk}}}
}

// bootArgs appends the guest network config, since firecracker does not set it.
func bootArgs(boot storage.BootConfig, nic network.NIC) string {
	ip := fmt.Sprintf("ip=%s::%s:255.255.255.0::eth0:off", nic.GuestIP, nic.GatewayIP)
	return boot.KernelArgs + " " + ip
}

func limits(spec vm.Spec) systemd.Limits {
	return systemd.Limits{
		MemoryMaxBytes: int64(spec.MemMiB) << 20,
		CPUQuotaPct:    spec.VCPUs * 100,
	}
}

// waitSocket blocks until the firecracker API socket appears or ctx is done.
func waitSocket(ctx context.Context, path string) error {
	for {
		if _, err := os.Stat(path); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
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
