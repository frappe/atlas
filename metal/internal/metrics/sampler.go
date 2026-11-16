package metrics

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// SampleInterval is the time between two samples of one VM.
const SampleInterval = 10 * time.Second
const sampleBudget = 9 * time.Second
const pruneInterval = time.Hour
const maximumConcurrentSamples = 16

// VirtualMachineSource lists VMs, reads their current use, and receives each stored record.
type VirtualMachineSource interface {
	ListIDs(context.Context) ([]string, error)
	Metrics(context.Context, string) (Sample, error)
	// RecordMetrics receives each record after the store keeps it. It runs in
	// the sweep workers. An error leaves the stored record in place.
	RecordMetrics(ctx context.Context, identifier string, record Record) error
}

// Sampler owns the periodic collection worker; Store owns persisted history.
type Sampler struct {
	store           *Store
	virtualMachines VirtualMachineSource
	logger          *slog.Logger
	previousDisk    map[string]diskSnapshot
	previousDiskMu  sync.Mutex
}

type diskSnapshot struct {
	counters DiskCounters
	time     time.Time
}

// NewSampler collects VM metrics from one host.
func NewSampler(store *Store, virtualMachines VirtualMachineSource, logger *slog.Logger) *Sampler {
	return &Sampler{store: store, virtualMachines: virtualMachines, logger: logger, previousDisk: make(map[string]diskSnapshot)}
}

// Run samples VMs every ten seconds and prunes old samples hourly.
func (sampler *Sampler) Run(ctx context.Context) {
	sampleTicker := time.NewTicker(SampleInterval)
	pruneTicker := time.NewTicker(pruneInterval)
	defer sampleTicker.Stop()
	defer pruneTicker.Stop()
	if err := sampler.store.Prune(ctx, time.Now()); err != nil {
		sampler.logger.Error("prune VM metrics", "error", err)
	}
	sampler.Collect(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-sampleTicker.C:
			sampler.Collect(ctx)
		case <-pruneTicker.C:
			if err := sampler.store.Prune(ctx, time.Now()); err != nil {
				sampler.logger.Error("prune VM metrics", "error", err)
			}
		}
	}
}

// Collect records one sample for each VM and continues after a per-VM failure.
func (sampler *Sampler) Collect(ctx context.Context) {
	if ctx.Err() != nil {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, sampleBudget)
	defer cancel()
	virtualMachineIDs, err := sampler.virtualMachines.ListIDs(ctx)
	if err != nil {
		sampler.logger.Warn("list VMs for metrics", "error", err)
		return
	}
	workQueue := make(chan string)
	var workers sync.WaitGroup
	for range min(maximumConcurrentSamples, len(virtualMachineIDs)) {
		workers.Go(func() {
			for identifier := range workQueue {
				usage, err := sampler.virtualMachines.Metrics(ctx, identifier)
				if err == nil && ctx.Err() == nil {
					timestamp := time.Now().UTC()
					sampler.previousDiskMu.Lock()
					usage = sampler.withDiskRates(identifier, timestamp, usage)
					sampler.previousDiskMu.Unlock()
					record := Record{Timestamp: timestamp, Metrics: usage}
					err = sampler.store.Append(identifier, record)
					if err == nil {
						if err := sampler.virtualMachines.RecordMetrics(ctx, identifier, record); err != nil {
							sampler.logger.Warn("keep VM CPU samples", "virtual_machine", identifier, "error", err)
						}
						sampler.previousDiskMu.Lock()
						if usage.Up {
							sampler.previousDisk[identifier] = diskSnapshot{counters: usage.Counters, time: timestamp}
						} else {
							delete(sampler.previousDisk, identifier)
						}
						sampler.previousDiskMu.Unlock()
					}
				}
			}
		})
	}
	for _, identifier := range virtualMachineIDs {
		select {
		case <-ctx.Done():
			close(workQueue)
			workers.Wait()
			sampler.logger.Warn("VM metrics sweep timed out", "virtual_machine_count", len(virtualMachineIDs))
			return
		case workQueue <- identifier:
		}
	}
	close(workQueue)
	workers.Wait()
	active := make(map[string]bool, len(virtualMachineIDs))
	for _, identifier := range virtualMachineIDs {
		active[identifier] = true
	}
	sampler.previousDiskMu.Lock()
	for identifier := range sampler.previousDisk {
		if !active[identifier] {
			delete(sampler.previousDisk, identifier)
		}
	}
	sampler.previousDiskMu.Unlock()
}

func (sampler *Sampler) withDiskRates(identifier string, timestamp time.Time, sample Sample) Sample {
	previous, found := sampler.previousDisk[identifier]
	if !sample.Up || !found || !timestamp.After(previous.time) {
		return sample
	}
	current := sample.Counters
	old := previous.counters
	if current.ReadBytes < old.ReadBytes || current.WriteBytes < old.WriteBytes ||
		current.ReadOperations < old.ReadOperations || current.WriteOperations < old.WriteOperations {
		return sample
	}
	milliseconds := timestamp.Sub(previous.time).Milliseconds()
	if milliseconds == 0 {
		return sample
	}
	sample.DiskReadBytesPerSecond = (current.ReadBytes - old.ReadBytes) * 1000 / uint64(milliseconds)
	sample.DiskWriteBytesPerSecond = (current.WriteBytes - old.WriteBytes) * 1000 / uint64(milliseconds)
	sample.DiskReadMilliIOPS = (current.ReadOperations - old.ReadOperations) * 1_000_000 / uint64(milliseconds)
	sample.DiskWriteMilliIOPS = (current.WriteOperations - old.WriteOperations) * 1_000_000 / uint64(milliseconds)
	return sample
}
