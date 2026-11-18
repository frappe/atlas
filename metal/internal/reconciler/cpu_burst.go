package reconciler

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// CPUBurstManager throttles and releases burstable VMs.
type CPUBurstManager interface {
	LimitCPUBursts(ctx context.Context) error
}

// CPUBurstLimiter applies the CPU limits of burstable VMs on an interval.
type CPUBurstLimiter struct {
	passScheduler

	manager CPUBurstManager
	logger  *slog.Logger
}

// NewCPUBurstLimiter returns a CPU burst limiter that runs at interval.
func NewCPUBurstLimiter(manager CPUBurstManager, interval time.Duration, registry *telemetry.Registry, logger *slog.Logger) *CPUBurstLimiter {
	if logger == nil {
		logger = slog.Default()
	}
	return &CPUBurstLimiter{passScheduler: newPassScheduler("cpu_burst", interval, registry), manager: manager, logger: logger}
}

// Run applies CPU limits until ctx is canceled.
func (limiter *CPUBurstLimiter) Run(ctx context.Context) {
	limiter.run(ctx, limiter.limit)
}

func (limiter *CPUBurstLimiter) limit(ctx context.Context) {
	if err := limiter.manager.LimitCPUBursts(ctx); err != nil && ctx.Err() == nil {
		limiter.countFailure()
		limiter.logger.Error("CPU burst limit failed", "error", err)
	}
}
