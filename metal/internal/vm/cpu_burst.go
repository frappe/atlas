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
		average = manager.slidingWindowUsage.average(identifier, time.Now(), burst.BaselineCPUMillicores, desired.CPUBurstResetAt).MilliCPUs
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
	case isLimited && average <= uint64(baseline*releasePercent/100):
		return cpuMillicores
	case isLimited:
		return baseline
	default:
		return cpuMillicores
	}
}
