package datum

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
)

const maximumConcurrentExports = 4

// VirtualMachineSource supplies the VMs on this host and their current metrics.
type VirtualMachineSource interface {
	ListIDs(ctx context.Context) ([]string, error)
	Metrics(ctx context.Context, id string) (vm.Metrics, error)
}

// HostCapacitySource reports current host capacity.
type HostCapacitySource interface {
	Capacity(ctx context.Context) (host.Capacity, error)
}

// ingester sends one sample batch under one resource's token.
type ingester interface {
	Ingest(ctx context.Context, token string, samples []Sample) error
}

// Exporter pushes one pass of host and per-VM samples to datum.
type Exporter struct {
	client          ingester
	tokenFilePath   string
	virtualMachines VirtualMachineSource
	host            HostCapacitySource
	pushTimeout     time.Duration
	logger          *slog.Logger
}

// NewExporter returns an exporter that reads tokens from tokenFilePath on
// every pass, so a re-shipped bundle takes effect on the next tick.
func NewExporter(
	client *Client,
	tokenFilePath string,
	virtualMachines VirtualMachineSource,
	host HostCapacitySource,
	pushTimeout time.Duration,
	logger *slog.Logger,
) *Exporter {
	if logger == nil {
		logger = slog.Default()
	}
	return &Exporter{
		client:          client,
		tokenFilePath:   tokenFilePath,
		virtualMachines: virtualMachines,
		host:            host,
		pushTimeout:     pushTimeout,
		logger:          logger,
	}
}

// PushAll exports the host and every VM the token bundle names. A VM the
// bundle has no token for yet (churn since the last ship) is skipped, not
// treated as an error.
func (exporter *Exporter) PushAll(ctx context.Context) {
	bundle, err := ReadTokenBundle(exporter.tokenFilePath)
	if err != nil {
		exporter.logger.Warn("datum export skipped", "error", err)
		return
	}

	var workers sync.WaitGroup
	defer workers.Wait()
	slots := make(chan struct{}, maximumConcurrentExports)
	launch := func(push func(context.Context)) bool {
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			return false
		}
		if ctx.Err() != nil {
			<-slots
			return false
		}
		workers.Go(func() {
			defer func() { <-slots }()
			pushContext, cancel := context.WithTimeout(ctx, exporter.pushTimeout)
			defer cancel()
			push(pushContext)
		})
		return true
	}

	if bundle.Host != "" && !launch(func(ctx context.Context) { exporter.pushHost(ctx, bundle.Host) }) {
		return
	}

	ids, err := exporter.virtualMachines.ListIDs(ctx)
	if err != nil {
		exporter.logger.Error("datum export failed to list VMs", "error", err)
		return
	}
	for _, id := range ids {
		token, ok := bundle.VMs[id]
		if !ok {
			continue
		}
		if !launch(func(ctx context.Context) { exporter.pushVirtualMachine(ctx, id, token) }) {
			return
		}
	}
}

func (exporter *Exporter) pushHost(ctx context.Context, token string) {
	capacity, err := exporter.host.Capacity(ctx)
	if err != nil {
		exporter.logger.Error("datum export failed to read host capacity", "error", err)
		return
	}

	now := time.Now()
	samples := []Sample{
		{Metric: "host_up", Value: 1, Timestamp: now},
		{Metric: "host_cpu_millicores_total", Value: float64(capacity.TotalCPUMillicores), Timestamp: now},
		{Metric: "host_cpu_millicores_available", Value: float64(capacity.AvailableCPUMillicores), Timestamp: now},
		{Metric: "host_memory_mib_total", Value: float64(capacity.TotalMemoryMiB), Timestamp: now},
		{Metric: "host_memory_mib_available", Value: float64(capacity.AvailableMemoryMiB), Timestamp: now},
		{Metric: "host_storage_mib_total", Value: float64(capacity.TotalStorageMiB), Timestamp: now},
		{Metric: "host_storage_mib_available", Value: float64(capacity.AvailableStorageMiB), Timestamp: now},
		{Metric: "host_virtual_machine_count", Value: float64(capacity.VirtualMachineCount), Timestamp: now},
	}

	exporter.send(ctx, token, samples, "host")
}

func (exporter *Exporter) pushVirtualMachine(ctx context.Context, id, token string) {
	metrics, err := exporter.virtualMachines.Metrics(ctx, id)
	if err != nil {
		exporter.logger.Error("datum export failed to read VM metrics", "virtual_machine_id", id, "error", err)
		return
	}

	up := 0.0
	if metrics.Up {
		up = 1
	}

	now := time.Now()
	samples := []Sample{
		{Metric: "vm_up", Value: up, Timestamp: now},
		{Metric: "vm_cpu_microseconds_total", Value: float64(metrics.CPUUsageMicroseconds), Timestamp: now},
		{Metric: "vm_memory_bytes", Value: float64(metrics.MemoryBytes), Timestamp: now},
		{Metric: "vm_disk_size_mib", Value: float64(metrics.DiskMiB), Timestamp: now},
		{Metric: "vm_disk_used_mib", Value: float64(metrics.DiskUsedMiB), Timestamp: now},
		{Metric: "vm_network_received_bytes_total", Value: float64(metrics.ReceivedBytes), Timestamp: now},
		{Metric: "vm_network_received_packets_total", Value: float64(metrics.ReceivedPackets), Timestamp: now},
		{Metric: "vm_network_sent_bytes_total", Value: float64(metrics.SentBytes), Timestamp: now},
		{Metric: "vm_network_sent_packets_total", Value: float64(metrics.SentPackets), Timestamp: now},
	}

	exporter.send(ctx, token, samples, id)
}

func (exporter *Exporter) send(ctx context.Context, token string, samples []Sample, resource string) {
	if err := exporter.client.Ingest(ctx, token, samples); err != nil {
		exporter.logger.Warn("datum export failed", "resource", resource, "error", err)
	}
}
