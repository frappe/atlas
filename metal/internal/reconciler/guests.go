package reconciler

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// GuestManager pings the guests of running VMs.
type GuestManager interface {
	CheckGuests(ctx context.Context) error
}

// GuestChecker pings every running guest on an interval, so a guest that stops
// answering counts as unavailable.
type GuestChecker struct {
	passScheduler

	manager GuestManager
	logger  *slog.Logger
}

// NewGuestChecker returns a guest checker that runs at interval.
func NewGuestChecker(manager GuestManager, interval time.Duration, registry *telemetry.Registry, logger *slog.Logger) *GuestChecker {
	if logger == nil {
		logger = slog.Default()
	}
	return &GuestChecker{passScheduler: newPassScheduler("guest_check", interval, registry), manager: manager, logger: logger}
}

// Run checks guests until ctx is canceled.
func (checker *GuestChecker) Run(ctx context.Context) {
	checker.run(ctx, checker.check)
}

func (checker *GuestChecker) check(ctx context.Context) {
	if err := checker.manager.CheckGuests(ctx); err != nil && ctx.Err() == nil {
		checker.countFailure()
		checker.logger.Error("guest check failed", "error", err)
	}
}
