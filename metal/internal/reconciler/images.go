package reconciler

import (
	"context"
	"log"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	defaultImageReconcileTimeout = 35 * time.Minute
	defaultImageMaximumIdle      = 24 * time.Hour
	defaultSnapshotMaximumIdle   = 48 * time.Hour
)

// ImageStore reconciles and prunes local image artifacts.
type ImageStore interface {
	ImagePolicies(ctx context.Context) ([]vm.ImageRef, error)
	EnsureImage(ctx context.Context, image vm.ImageRef) error
	PruneImages(ctx context.Context, policies []vm.ImageRef, now time.Time, maximumIdle time.Duration) error
}

// SnapshotStore prunes local snapshot staging.
type SnapshotStore interface {
	PruneStagedSnapshots(ctx context.Context, now time.Time, maximumIdle time.Duration) error
}

// MemorySnapshotBuilder creates local warm boot artifacts.
type MemorySnapshotBuilder interface {
	EnsureMemorySnapshot(ctx context.Context, image vm.ImageRef) error
}

// ImageConfig controls image reconciliation.
type ImageConfig struct {
	OperationTimeout    time.Duration
	ImageMaximumIdle    time.Duration
	SnapshotMaximumIdle time.Duration
}

// ImageReconciler maintains controller-selected images on one host.
type ImageReconciler struct {
	imageStore          ImageStore
	snapshotStore       SnapshotStore
	builder             MemorySnapshotBuilder
	interval            time.Duration
	operationTimeout    time.Duration
	imageMaximumIdle    time.Duration
	snapshotMaximumIdle time.Duration
	wake                chan struct{}
}

// NewImageReconciler returns an image reconciler.
func NewImageReconciler(
	imageStore ImageStore,
	snapshotStore SnapshotStore,
	builder MemorySnapshotBuilder,
	interval time.Duration,
	configuration ImageConfig,
) *ImageReconciler {
	if configuration.OperationTimeout <= 0 {
		configuration.OperationTimeout = defaultImageReconcileTimeout
	}
	if configuration.ImageMaximumIdle <= 0 {
		configuration.ImageMaximumIdle = defaultImageMaximumIdle
	}
	if configuration.SnapshotMaximumIdle <= 0 {
		configuration.SnapshotMaximumIdle = defaultSnapshotMaximumIdle
	}
	return &ImageReconciler{
		imageStore:          imageStore,
		snapshotStore:       snapshotStore,
		builder:             builder,
		interval:            interval,
		operationTimeout:    configuration.OperationTimeout,
		imageMaximumIdle:    configuration.ImageMaximumIdle,
		snapshotMaximumIdle: configuration.SnapshotMaximumIdle,
		wake:                make(chan struct{}, 1),
	}
}

// Wake requests an image reconcile pass without blocking the caller.
func (reconciler *ImageReconciler) Wake() {
	select {
	case reconciler.wake <- struct{}{}:
	default:
	}
}

// Run maintains images until the context is canceled.
func (reconciler *ImageReconciler) Run(ctx context.Context) {
	ticker := time.NewTicker(reconciler.interval)
	defer ticker.Stop()
	for {
		reconciler.reconcileAll(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-reconciler.wake:
		}
	}
}

func (reconciler *ImageReconciler) reconcileAll(ctx context.Context) {
	listContext, cancelList := context.WithTimeout(ctx, reconciler.operationTimeout)
	policies, err := reconciler.imageStore.ImagePolicies(listContext)
	cancelList()
	if err != nil {
		reconciler.logError(ctx, "load policies", err)
		return
	}

	for _, image := range policies {
		if !image.CacheImage {
			continue
		}
		reconciler.reconcileImage(ctx, image)
	}

	pruneContext, cancelPrune := context.WithTimeout(ctx, reconciler.operationTimeout)
	now := time.Now()
	err = reconciler.imageStore.PruneImages(pruneContext, policies, now, reconciler.imageMaximumIdle)
	if err == nil {
		err = reconciler.snapshotStore.PruneStagedSnapshots(
			pruneContext,
			now,
			reconciler.snapshotMaximumIdle,
		)
	}
	cancelPrune()
	if err != nil {
		reconciler.logError(ctx, "prune local artifacts", err)
	}
}

func (reconciler *ImageReconciler) reconcileImage(ctx context.Context, image vm.ImageRef) {
	operationContext, cancel := context.WithTimeout(ctx, reconciler.operationTimeout)
	defer cancel()

	if err := reconciler.imageStore.EnsureImage(operationContext, image); err != nil {
		reconciler.logError(ctx, "cache image "+image.Name, err)
		return
	}
	if image.MemorySnapshot {
		if err := reconciler.builder.EnsureMemorySnapshot(operationContext, image); err != nil {
			reconciler.logError(ctx, "warm image "+image.Name, err)
		}
	}
}

func (reconciler *ImageReconciler) logError(ctx context.Context, operation string, err error) {
	if ctx.Err() == nil {
		log.Printf("image reconciler: %s: %v", operation, err)
	}
}
