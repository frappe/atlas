package firecracker

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	ifaceID     = "eth0"
	mmdsAddr    = "169.254.169.254"
	mmdsVersion = "V1" // simplest guest-side (plain GET); V2 adds token auth

	rootDriveID   = "drive0"      // the first (root) drive, see configure
	rootDrivePath = "/rootfs.img" // the in-chroot block node, see storage.Prepare
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

// refreshMMDS builds the metadata a restored clone reads: the ssh keys plus a
// generation token under "metal". A guest agent watches the token to re-key and
// re-sync (ssh keys, clock, machine-id) after a warm load. The token is the new
// VM id, which differs from the source, so every clone sees a change.
func refreshMMDS(id string, keys []string) map[string]any {
	data := mmdsData(keys)
	data["metal"] = map[string]any{"generation": id}
	return data
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
