package metrics

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
)

type VirtualMachineSource interface {
	ListIDs(context.Context) ([]string, error)
	Metrics(context.Context, string) (vm.Metrics, error)
}

type HostSource interface {
	Capacity(context.Context) (host.Capacity, error)
}

// Sampler owns the periodic collection worker; Store owns persisted history.
type Sampler struct {
	store           *Store
	virtualMachines VirtualMachineSource
	host            HostSource
	logger          *slog.Logger
}

func NewSampler(store *Store, virtualMachines VirtualMachineSource, host HostSource, logger *slog.Logger) *Sampler {
	return &Sampler{store: store, virtualMachines: virtualMachines, host: host, logger: logger}
}

func (sampler *Sampler) Run(ctx context.Context) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		sampler.Collect(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (sampler *Sampler) Collect(ctx context.Context) {
	if err := sampler.store.Prune(ctx, time.Now()); err != nil {
		sampler.logger.Error("prune metrics", "error", err)
	}
	if ctx.Err() != nil {
		return
	}
	capacity, err := sampler.host.Capacity(ctx)
	if err == nil {
		err = sampler.store.Append("host", Record{Timestamp: time.Now().UTC(), Host: &capacity})
	}
	if err != nil {
		sampler.logger.Warn("collect host metrics", "error", err)
	}
	ids, err := sampler.virtualMachines.ListIDs(ctx)
	if err != nil {
		sampler.logger.Warn("list VMs for metrics", "error", err)
		return
	}
	for _, id := range ids {
		if ctx.Err() != nil {
			return
		}
		usage, err := sampler.virtualMachines.Metrics(ctx, id)
		if err == nil {
			err = sampler.store.Append("vm:"+id, Record{Timestamp: time.Now().UTC(), VM: &usage})
		}
		if err != nil {
			sampler.logger.Warn("collect VM metrics", "virtual_machine_id", id, "error", err)
		}
	}
}
