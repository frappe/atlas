package vm

import (
	"context"
	"errors"

	"github.com/frappe/atlas/metal/internal/metrics"
	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// Metrics returns the current resource use of the virtual machine identified
// by identifier.
func (manager *Manager) Metrics(ctx context.Context, identifier string) (metrics.Sample, error) {
	if manager.isDestinationReserved(identifier) {
		return metrics.Sample{}, ErrNotFound
	}

	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return metrics.Sample{}, err
	}

	diskMiB := observed.Disk.SizeMiB
	if diskMiB == 0 {
		diskMiB = desired.Specification.DiskMiB
	}
	up := observed.State == StateRunning || observed.State == StatePaused
	sample := metrics.Sample{Up: up, DiskUsage: metrics.DiskUsage{
		DiskMiB: diskMiB, DiskUsedMiB: observed.Disk.UsedMiB,
		DiskThroughputLimitMiBps: desired.Specification.Disk.ThroughputMiBps,
		DiskIOPSLimit:            desired.Specification.Disk.IOPS,
	}}

	if up {
		usage, err := manager.runtime.GetUsage(ctx, RuntimeMachine{ID: desired.ID})
		if err != nil {
			return metrics.Sample{}, err
		}
		sample.CPUTimeMicroseconds = usage.CPUTimeMicroseconds
		sample.MemoryBytes = usage.MemoryBytes
		sample.Counters = metrics.DiskCounters{
			ReadBytes: usage.DiskReadBytes, WriteBytes: usage.DiskWriteBytes,
			ReadOperations: usage.DiskReadOperations, WriteOperations: usage.DiskWriteOperations,
		}
	}

	if manager.traffic != nil {
		target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
		received, sent, err := manager.traffic.ReadTrafficCounters(target)
		if err != nil && !errors.Is(err, traffic.ErrNotFound) {
			return metrics.Sample{}, err
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
