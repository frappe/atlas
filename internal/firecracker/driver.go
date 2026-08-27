// Package firecracker implements vm.VMDriver on Firecracker. Each VM is a
// jailer'd firecracker process run as a systemd template unit; metald is only a
// client, talking to systemd (D-Bus) and each VM's API socket.
package firecracker

import (
	"context"
	"errors"

	"github.com/frappe/metal/internal/network"
	"github.com/frappe/metal/internal/storage"
	"github.com/frappe/metal/internal/systemd"
	"github.com/frappe/metal/internal/vm"
)

var errNotImplemented = errors.New("firecracker: not implemented")

type Driver struct {
	units   systemd.Manager
	images  storage.Resolver
	net     network.Allocator
	baseDir string // jailer chroot base dir
}

func New(units systemd.Manager, images storage.Resolver, net network.Allocator, baseDir string) *Driver {
	return &Driver{units: units, images: images, net: net, baseDir: baseDir}
}

func (d *Driver) Create(ctx context.Context, spec vm.Spec) (vm.VM, error) {
	return nil, errNotImplemented
}

func (d *Driver) Load(ctx context.Context, id string) (vm.VM, error) {
	return nil, errNotImplemented
}

func (d *Driver) List(ctx context.Context) ([]vm.VM, error) {
	return nil, errNotImplemented
}

func (d *Driver) Type() vm.DriverType { return vm.DriverFirecracker }

var _ vm.VMDriver = (*Driver)(nil)
