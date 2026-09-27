package reconciler

import (
	"context"
	"time"
)

// DatumExporter pushes one pass of host and per-VM metrics to datum. It owns
// every export decision and its own error logging.
type DatumExporter interface {
	PushAll(ctx context.Context)
}

// DatumReconciler exports metrics to datum on an interval.
type DatumReconciler struct {
	passScheduler

	exporter DatumExporter
}

// NewDatumReconciler returns a datum reconciler that runs at interval.
func NewDatumReconciler(exporter DatumExporter, interval time.Duration) *DatumReconciler {
	return &DatumReconciler{passScheduler: newPassScheduler(interval), exporter: exporter}
}

// Run exports metrics until ctx is canceled.
func (r *DatumReconciler) Run(ctx context.Context) {
	r.run(ctx, r.exporter.PushAll)
}
