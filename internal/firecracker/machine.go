package firecracker

import (
	"context"

	"github.com/frappe/metal/internal/firecracker/api"
	"github.com/frappe/metal/internal/systemd"
	"github.com/frappe/metal/internal/vm"
)

// machine implements vm.VM as a client-side handle: a systemd unit (metal-vm@id)
// plus an API client over that unit's socket. It holds no child process.
type machine struct {
	id    string
	units systemd.Manager
	api   *api.Client
}

func (m *machine) ID() string { return m.id }

func (m *machine) Start(ctx context.Context) error            { return errNotImplemented }
func (m *machine) Stop(ctx context.Context, force bool) error { return errNotImplemented }
func (m *machine) Destroy(ctx context.Context) error          { return errNotImplemented }

func (m *machine) Wait(ctx context.Context) (vm.ExitStatus, error) {
	return vm.ExitStatus{}, errNotImplemented
}

func (m *machine) Info(ctx context.Context) (vm.Info, error) {
	return vm.Info{}, errNotImplemented
}

// Snapshot is deferred: the current milestone excludes snapshotting.
func (m *machine) Snapshot(ctx context.Context, dir string, typ vm.SnapshotType) (vm.Snapshot, error) {
	return vm.Snapshot{}, errNotImplemented
}

var _ vm.VM = (*machine)(nil)
