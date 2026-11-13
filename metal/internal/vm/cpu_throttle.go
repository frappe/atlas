package vm

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/frappe/atlas/metal/internal/metrics"
)

// cpuReleasePercent is the share of the baseline that a throttled VM must
// average below before it gets its full CPU back. Without the gap, a VM held
// at its baseline averages the baseline and switches on every pass.
const cpuReleasePercent = 90

// CPUThrottler holds a running burstable VM at its CPU baseline while its
// average CPU use in the window is above the baseline. Throttle must not run
// concurrently, because it owns the throttled set.
type CPUThrottler struct {
	manager   *Manager
	window    *metrics.CPUWindow
	throttled map[string]bool
}

// NewCPUThrottler returns a throttler that reads CPU use from window.
func NewCPUThrottler(manager *Manager, window *metrics.CPUWindow) *CPUThrottler {
	return &CPUThrottler{manager: manager, window: window, throttled: make(map[string]bool)}
}

// Throttle applies the CPU limit of each running burstable VM. It continues
// after a failure for one VM.
func (throttler *CPUThrottler) Throttle(ctx context.Context) error {
	identifiers, err := throttler.manager.ListIDs(ctx)
	if err != nil {
		return err
	}

	var errs []error
	active := make(map[string]bool, len(identifiers))
	for _, identifier := range identifiers {
		active[identifier] = true
		if err := throttler.throttle(ctx, identifier); err != nil {
			errs = append(errs, fmt.Errorf("throttle the CPU of %s: %w", identifier, err))
		}
	}

	for identifier := range throttler.throttled {
		if !active[identifier] {
			delete(throttler.throttled, identifier)
		}
	}
	return errors.Join(errs...)
}

// throttle sets the CPU limit of one VM. It applies the limit on every pass,
// because a boot resets the unit to the full CPU.
func (throttler *CPUThrottler) throttle(ctx context.Context, identifier string) error {
	if throttler.manager.isDestinationReserved(identifier) {
		return nil
	}
	desired, observed, err := throttler.manager.newVirtualMachine(identifier).records()
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	specification := desired.Specification
	if !specification.IsBurstable || observed.State != StateRunning {
		delete(throttler.throttled, identifier)
		return nil
	}

	baseline := specification.CPUBaseline()
	baselinePercent := 100 * float64(baseline) / float64(specification.CPUMillicores)
	wasThrottled := throttler.throttled[identifier]
	isThrottled := wasThrottled
	start := time.Now().Add(-throttler.window.Length())
	if desired.CPUThrottleResetAt.After(start) {
		start = desired.CPUThrottleResetAt
	}
	usagePercent, hasUsage := throttler.window.Usage(identifier, start).Percent(specification.CPUMillicores)
	switch {
	case !hasUsage:
		// A VM that just booted or was reset has no samples to hold it back.
		isThrottled = false
	case usagePercent > baselinePercent:
		isThrottled = true
	case usagePercent < baselinePercent*cpuReleasePercent/100:
		isThrottled = false
	}
	if isThrottled != wasThrottled {
		throttler.manager.logger.InfoContext(ctx, "burstable VM CPU throttle changed",
			"virtual_machine_id", identifier, "is_throttled", isThrottled,
			"cpu_usage_percent", usagePercent, "cpu_baseline_millicores", baseline)
	}
	throttler.throttled[identifier] = isThrottled

	machine := runtimeMachine(desired, observed.NetworkInterface)
	if isThrottled {
		machine.Specification.CPUMillicores = baseline
	}
	return throttler.manager.runtime.RefreshCPU(ctx, machine)
}
