package vm

import (
	"context"
	"errors"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

// Metrics describes the current resource use of one virtual machine. CPU and
// network are cumulative counters; memory is a point-in-time gauge; disk
// usage is as fresh as the last reconcile pass. Network counters last for the
// traffic attachment's lifetime, including while the guest is stopped.
type Metrics struct {
	Up                   bool
	DiskMiB              int
	DiskUsedMiB          int
	CPUUsageMicroseconds uint64
	MemoryBytes          uint64
	ReceivedBytes        uint64
	ReceivedPackets      uint64
	SentBytes            uint64
	SentPackets          uint64
}

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
	metrics := Metrics{Up: up, DiskMiB: diskMiB, DiskUsedMiB: observed.Disk.UsedMiB}

	if up {
		usage, err := manager.runtime.GetUsage(ctx, RuntimeMachine{ID: desired.ID})
		if err != nil {
			return Metrics{}, err
		}
		metrics.CPUUsageMicroseconds = usage.CPUUsageMicroseconds
		metrics.MemoryBytes = usage.MemoryBytes
	}

	if manager.traffic != nil {
		target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
		received, sent, err := manager.traffic.Counters(target)
		if err != nil && !errors.Is(err, traffic.ErrNotFound) {
			return Metrics{}, err
		}
		metrics.ReceivedBytes = received.Bytes
		metrics.ReceivedPackets = received.Packets
		metrics.SentBytes = sent.Bytes
		metrics.SentPackets = sent.Packets
	}

	return metrics, nil
}
