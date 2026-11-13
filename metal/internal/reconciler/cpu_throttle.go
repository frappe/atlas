package reconciler

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// CPUThrottle applies the CPU limits of burstable VMs.
type CPUThrottle interface {
	Throttle(ctx context.Context) error
}

// CPUThrottleChecker applies the CPU limits of burstable VMs on an interval.
type CPUThrottleChecker struct {
	passScheduler

	throttle CPUThrottle
	logger   *slog.Logger
}

// NewCPUThrottleChecker returns a CPU throttle checker that runs at interval.
func NewCPUThrottleChecker(throttle CPUThrottle, interval time.Duration, registry *telemetry.Registry, logger *slog.Logger) *CPUThrottleChecker {
	if logger == nil {
		logger = slog.Default()
	}
	return &CPUThrottleChecker{passScheduler: newPassScheduler("cpu_throttle", interval, registry), throttle: throttle, logger: logger}
}

// Run applies CPU limits until ctx is canceled.
func (checker *CPUThrottleChecker) Run(ctx context.Context) {
	checker.run(ctx, checker.check)
}

func (checker *CPUThrottleChecker) check(ctx context.Context) {
	if err := checker.throttle.Throttle(ctx); err != nil && ctx.Err() == nil {
		checker.countFailure()
		checker.logger.Error("CPU throttle failed", "error", err)
	}
}
