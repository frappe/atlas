package metrics

import (
	"context"
	"time"
)

// CPUUsage is the CPU time a virtual machine used while it was up.
type CPUUsage struct {
	CPUSeconds float64
	UpSeconds  int64
}

// Percent returns CPU use as a percent of cpuMillicores. It returns false
// when the window holds fewer than two neighbouring up samples.
func (usage CPUUsage) Percent(cpuMillicores int) (float64, bool) {
	if usage.UpSeconds == 0 || cpuMillicores <= 0 {
		return 0, false
	}
	allocatedCores := float64(cpuMillicores) / 1000
	return usage.CPUSeconds / float64(usage.UpSeconds) / allocatedCores * 100, true
}

// SlidingWindowCPUUsage returns the CPU use of one virtual machine in the
// window that ends now.
func (store *Store) SlidingWindowCPUUsage(ctx context.Context, identifier string, window time.Duration) (CPUUsage, error) {
	end := time.Now().UTC()
	records, err := store.History(ctx, identifier, end.Add(-window), end)
	if err != nil {
		return CPUUsage{}, err
	}
	return MeasureCPUUsage(records), nil
}

// MeasureCPUUsage adds the CPU time between neighbouring records in which the
// virtual machine was up. Records must be in timestamp order.
func MeasureCPUUsage(records []Record) CPUUsage {
	var usage CPUUsage
	for index := 1; index < len(records); index++ {
		previous, current := records[index-1], records[index]
		// A restart creates a new cgroup, which resets the counter.
		if !previous.Metrics.Up || !current.Metrics.Up || current.Metrics.CPUTimeMicroseconds < previous.Metrics.CPUTimeMicroseconds {
			continue
		}

		usage.CPUSeconds += float64(current.Metrics.CPUTimeMicroseconds-previous.Metrics.CPUTimeMicroseconds) / 1e6
		usage.UpSeconds += current.Timestamp.Unix() - previous.Timestamp.Unix()
	}
	return usage
}
