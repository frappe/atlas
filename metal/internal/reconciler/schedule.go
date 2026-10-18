package reconciler

import (
	"context"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// passScheduler runs one reconcile pass on an interval and on demand. Every
// reconciler embeds it, so they share one wake, shutdown, and metric behavior.
type passScheduler struct {
	loop      string
	interval  time.Duration
	wake      chan struct{}
	telemetry *telemetry.Registry
}

// newPassScheduler returns a scheduler for loop that ticks at interval.
func newPassScheduler(loop string, interval time.Duration, registry *telemetry.Registry) passScheduler {
	return passScheduler{loop: loop, interval: interval, wake: make(chan struct{}, 1), telemetry: registry}
}

// countFailure counts one failed item or list call of the loop.
func (s *passScheduler) countFailure() {
	s.telemetry.Count("metal_reconciler_failures_total", 1, telemetry.Labels{"loop": s.loop})
}

// Wake requests a pass without blocking the caller. A pending request absorbs
// further calls, so many wakes collapse into one pass.
func (s *passScheduler) Wake() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// run calls pass once, then again on every tick or wake, until ctx is canceled.
func (s *passScheduler) run(ctx context.Context, pass func(context.Context)) {
	ticker := time.NewTicker(s.interval)
	defer ticker.Stop()

	for {
		startedAt := time.Now()
		pass(ctx)
		labels := telemetry.Labels{"loop": s.loop}
		s.telemetry.Count("metal_reconciler_passes_total", 1, labels)
		s.telemetry.Count("metal_reconciler_pass_duration_seconds_total", time.Since(startedAt).Seconds(), labels)
		s.telemetry.Set("metal_reconciler_last_pass_timestamp_seconds", float64(time.Now().Unix()), labels)

		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-s.wake:
		}
	}
}
