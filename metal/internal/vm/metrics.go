package vm

import (
	"context"
	"errors"

	"github.com/frappe/atlas/metal/internal/metrics"
	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// Metrics is one resource-use snapshot supplied to the metrics sampler.
type Metrics = metrics.Sample

// Metrics returns the current resource use of the virtual machine identified
// by identifier.
func (manager *Manager) Metrics(ctx context.Context, identifier string) (Metrics, error) {
	if manager.isDestinationReserved(identifier) {
		return Metrics{}, ErrNotFound
	}

	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return Metrics{}, err
	}

	diskMiB := observed.Disk.SizeMiB
	if diskMiB == 0 {
		diskMiB = desired.Specification.DiskMiB
	}
	up := observed.State == StateRunning || observed.State == StatePaused
	sample := Metrics{Up: up, DiskUsage: metrics.DiskUsage{
		DiskMiB: diskMiB, DiskUsedMiB: observed.Disk.UsedMiB,
		DiskThroughputLimitMiBps: desired.Specification.Disk.ThroughputMiBps,
		DiskIOPSLimit:            desired.Specification.Disk.IOPS,
	}}

	if up {
		usage, err := manager.runtime.GetUsage(ctx, RuntimeMachine{ID: desired.ID})
		if err != nil {
			return Metrics{}, err
		}
		sample.CPUUsageMicroseconds = usage.CPUUsageMicroseconds
		sample.MemoryBytes = usage.MemoryBytes
		sample.Counters = metrics.DiskCounters{
			ReadBytes: usage.DiskReadBytes, WriteBytes: usage.DiskWriteBytes,
			ReadOperations: usage.DiskReadOperations, WriteOperations: usage.DiskWriteOperations,
		}
	}

	if manager.traffic != nil {
		target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
		received, sent, err := manager.traffic.Counters(target)
		if err != nil && !errors.Is(err, traffic.ErrNotFound) {
			return Metrics{}, err
		}
		sample.ReceivedBytes = received.Bytes
		sample.ReceivedPackets = received.Packets
		sample.SentBytes = sent.Bytes
		sample.SentPackets = sent.Packets
		sample.SentICMPPackets = sent.ICMPPackets
		sample.SentUDPPackets = sent.UDPPackets
		sample.SentTCPSYNPackets = sent.TCPSYNPackets
		sample.SentTCPRSTPackets = sent.TCPRSTPackets
	}

	return sample, nil
}
