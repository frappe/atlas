package vm

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// SlidingWindowUsage is a VM's average resource use over a recent window.
type SlidingWindowUsage struct {
	// MilliCPUs is average CPU use in thousandths of a CPU. 1500 means 1.5 CPUs.
	MilliCPUs uint64
}

// SlidingWindowUsage returns a VM's average use over the window that ends now.
// It averages only over intervals where the VM was up at both samples.
func (manager *Manager) SlidingWindowUsage(ctx context.Context, identifier string, window time.Duration) (SlidingWindowUsage, error) {
	if manager.metrics == nil {
		return SlidingWindowUsage{}, errors.New("VM metrics store is not configured")
	}

	now := time.Now()
	records, err := manager.metrics.History(ctx, identifier, now.Add(-window), now)
	if err != nil {
		return SlidingWindowUsage{}, fmt.Errorf("read metrics history: %w", err)
	}

	var cpuMicroseconds uint64
	var upTime time.Duration
	for index := 1; index < len(records); index++ {
		previous, current := records[index-1].Metrics, records[index].Metrics
		// A restart recreates the cgroup and resets its CPU counter.
		if !previous.Up || !current.Up || current.CPUTimeMicroseconds < previous.CPUTimeMicroseconds {
			continue
		}
		cpuMicroseconds += current.CPUTimeMicroseconds - previous.CPUTimeMicroseconds
		upTime += records[index].Timestamp.Sub(records[index-1].Timestamp)
	}

	if upTime <= 0 {
		return SlidingWindowUsage{}, ErrNotEnoughSamples
	}
	return SlidingWindowUsage{MilliCPUs: cpuMicroseconds * 1000 / uint64(upTime.Microseconds())}, nil
}
