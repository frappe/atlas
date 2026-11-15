package vm

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// cpuLimitTimeout bounds the systemd calls of one VM, which run under its operation lock.
const cpuLimitTimeout = 5 * time.Second

// releasePercent is the share of the baseline that the average must fall to before
// a throttled VM gets its full CPU again. The gap stops a busy VM from switching
// on every pass.
const releasePercent = 90

// ReleaseCPUMillicores returns the average at or below which a limited VM gets its full CPU again.
func ReleaseCPUMillicores(baselineCPUMillicores int) int {
	return baselineCPUMillicores * releasePercent / 100
}

// ResetCPUBurst stores now as the VM's CPU burst reset time, so the sliding window
// average ignores earlier samples. The current CPU limit stays.
func (manager *Manager) ResetCPUBurst(ctx context.Context, identifier string) error {
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if !record.Specification.CPUBurst.IsBurstable {
			return false, ErrNotBurstable
		}
		// Samples have whole seconds, so a sample in the reset second counts.
		record.CPUBurstResetAt = time.Now().UTC().Truncate(time.Second)
		return true, nil
	})
}

// CPUBurstStatus is a burstable VM's sliding window average and current CPU limit.
type CPUBurstStatus struct {
	CPUBurst
	CPUMillicores int
	ResetAt       time.Time
	// Usage is nil until metald has CPU samples of the VM in memory.
	Usage *SlidingWindowUsage
	// CPULimit is the runtime CPU quota in millicores. It is nil when the VM is not running.
	CPULimit *int
}

// CPUBurstStatus reads a burstable VM's status. It takes no lock, so it can
// race with the limiter by one pass. After a window change, the old window
// applies until the next sweep.
func (manager *Manager) CPUBurstStatus(ctx context.Context, identifier string) (CPUBurstStatus, error) {
	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return CPUBurstStatus{}, err
	}
	if !desired.Specification.CPUBurst.IsBurstable {
		return CPUBurstStatus{}, ErrNotBurstable
	}

	status := CPUBurstStatus{
		CPUBurst:      desired.Specification.CPUBurst,
		CPUMillicores: desired.Specification.CPUMillicores,
		ResetAt:       desired.CPUBurstResetAt,
	}
	usage, err := manager.slidingWindowUsage.average(identifier, time.Now(), desired.CPUBurstResetAt)
	if err == nil {
		status.Usage = &usage
	} else if !errors.Is(err, ErrNotEnoughSamples) {
		return CPUBurstStatus{}, err
	}

	if desired.State == StateRunning && observed.State == StateRunning {
		ctx, cancel := context.WithTimeout(ctx, cpuLimitTimeout)
		defer cancel()
		limit, err := manager.runtime.CPULimit(ctx, runtimeMachine(desired, observed.NetworkInterface))
		if err != nil {
			return CPUBurstStatus{}, fmt.Errorf("read CPU limit of VM %s: %w", identifier, err)
		}
		status.CPULimit = &limit
	}
	return status, nil
}

// LimitCPUBursts throttles each running burstable VM whose average CPU use is above
// its baseline, and releases it when the average falls to 90% of the baseline.
// One VM's error does not stop the others.
func (manager *Manager) LimitCPUBursts(ctx context.Context) error {
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return err
	}

	var errs []error
	for _, identifier := range identifiers {
		if ctx.Err() != nil {
			break
		}
		errs = append(errs, manager.limitCPUBurst(ctx, identifier))
	}
	return errors.Join(errs...)
}

// limitCPUBurst applies one VM's CPU limit. The cgroup quota is the throttle
// state, so a metald or VM restart needs no repair. A VM that stops being
// burstable is released.
func (manager *Manager) limitCPUBurst(ctx context.Context, identifier string) error {
	// The lock keeps a start, which sets the full quota, out of the read and the write.
	unlock, acquired := manager.operationLocks.TryLock(identifier)
	if !acquired {
		return nil
	}
	defer unlock()
	if manager.isMigrating(identifier) {
		return nil
	}

	virtualMachine := manager.newVirtualMachine(identifier)
	desired, observed, err := virtualMachine.records()
	if virtualMachine.isRemoved(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if desired.State != StateRunning || observed.State != StateRunning {
		return nil
	}

	ctx, cancel := context.WithTimeout(ctx, cpuLimitTimeout)
	defer cancel()
	machine := runtimeMachine(desired, observed.NetworkInterface)
	current, err := manager.runtime.CPULimit(ctx, machine)
	if err != nil {
		return fmt.Errorf("read CPU limit of VM %s: %w", identifier, err)
	}

	burst := desired.Specification.CPUBurst
	target := desired.Specification.CPUMillicores
	var average uint64
	if burst.IsBurstable {
		usage, err := manager.slidingWindowUsage.average(identifier, time.Now(), desired.CPUBurstResetAt)
		// Until the first sweep after a metald restart builds the ring, keep the current limit.
		if errors.Is(err, ErrNotEnoughSamples) {
			return nil
		}
		if err != nil {
			return err
		}
		average = usage.MilliCPUs
		target = cpuLimit(burst, desired.Specification.CPUMillicores, current, average)
	}
	if target == current {
		return nil
	}

	if err := manager.runtime.SetCPULimit(ctx, machine, target); err != nil {
		return fmt.Errorf("set CPU limit of VM %s: %w", identifier, err)
	}
	message := "release VM CPU"
	if target < desired.Specification.CPUMillicores {
		message = "limit VM CPU"
	}
	manager.logger.Info(message, "virtual_machine_id", identifier, "average_millicpus", average, "limit_millicores", target)
	return nil
}

// cpuLimit returns the CPU quota that a burstable VM should have.
//
//	average (baseline 1000, CPU 2000)
//	  > 1000 while released   → 1000  throttle
//	  ≤  900 while limited    → 2000  release
//	  otherwise               → keep the state; a limited VM gets the current baseline
func cpuLimit(burst CPUBurst, cpuMillicores, current int, average uint64) int {
	baseline := burst.BaselineCPUMillicores
	isLimited := current < cpuMillicores
	switch {
	case !isLimited && average > uint64(baseline):
		return baseline
	case isLimited && average <= uint64(ReleaseCPUMillicores(baseline)):
		return cpuMillicores
	case isLimited:
		return baseline
	default:
		return cpuMillicores
	}
}
