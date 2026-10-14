package telemetry

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

const (
	pushesMetric              = "metal_telemetry_pushes_total"
	samplesSentMetric         = "metal_telemetry_samples_sent_total"
	droppedObservationsMetric = "metal_telemetry_observations_dropped_total"
	collectorErrorsMetric     = "metal_telemetry_collector_errors_total"
)

// Pusher sends one registry snapshot to Datum on every interval.
type Pusher struct {
	registry *Registry
	client   *Client
	interval time.Duration
	logger   *slog.Logger
	failing  bool
}

// NewPusher returns a pusher for registry.
func NewPusher(registry *Registry, client *Client, interval time.Duration, logger *slog.Logger) *Pusher {
	return &Pusher{registry: registry, client: client, interval: interval, logger: logger}
}

// Run pushes until ctx is canceled.
func (pusher *Pusher) Run(ctx context.Context) {
	ticker := time.NewTicker(pusher.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			pusher.Push(ctx, now.UTC())
		}
	}
}

// Push sends one snapshot and the pending observations.
func (pusher *Pusher) Push(ctx context.Context, now time.Time) {
	samples := pusher.registry.Snapshot(ctx, now)
	observations := pusher.registry.TakeObservations()

	pushContext, cancel := context.WithTimeout(ctx, pusher.interval)
	defer cancel()
	err := pusher.client.Send(pushContext, append(samples, observations...))
	if err != nil {
		pusher.registry.RestoreObservations(observations)
	}

	switch {
	case errors.Is(err, ErrNotConfigured):
		pusher.reportState(err, "telemetry has no Datum credentials")
		pusher.registry.Count(pushesMetric, 1, Labels{"result": "not_configured"})
	case err != nil:
		pusher.reportState(err, "telemetry push failed")
		pusher.registry.Count(pushesMetric, 1, Labels{"result": "failed"})
	default:
		pusher.reportState(nil, "")
		pusher.registry.Count(pushesMetric, 1, Labels{"result": "ok"})
		pusher.registry.Count(samplesSentMetric, float64(len(samples)+len(observations)), nil)
	}
}

func (pusher *Pusher) reportState(err error, message string) {
	wasFailing := pusher.failing
	pusher.failing = err != nil
	switch {
	case err != nil && !wasFailing:
		pusher.logger.Warn(message, "component", "telemetry", "error", err)
	case err == nil && wasFailing:
		pusher.logger.Info("telemetry push recovered", "component", "telemetry")
	}
}
