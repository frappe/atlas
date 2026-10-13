package reconciler

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	// defaultImageReconcileTimeout bounds one image operation. Caching an image
	// downloads and imports it, so the limit is generous.
	defaultImageReconcileTimeout = 35 * time.Minute

	// defaultImageMaximumIdle is how long an image that host sync does not cache
	// stays on the host after its last VM start.
	defaultImageMaximumIdle = time.Hour

	// defaultSnapshotMaximumIdle is how long unused staging stays on the host.
	defaultSnapshotMaximumIdle = 48 * time.Hour
)

// ImageStore reconciles and prunes local image artifacts.
type ImageStore interface {
	ImagePolicies(ctx context.Context) ([]vm.Image, error)
	EnsureImage(ctx context.Context, image vm.Image) error
	RemoveWarmImages(ctx context.Context, imageReference string) error
	PruneImages(ctx context.Context, policies []vm.Image, now time.Time, maximumIdle time.Duration) error
}

// SnapshotStore prunes local snapshot staging.
type SnapshotStore interface {
	PruneStagedSnapshots(ctx context.Context, now time.Time, maximumIdle time.Duration) error
}

// MemorySnapshotBuilder creates local warm boot artifacts.
type MemorySnapshotBuilder interface {
	EnsureMemorySnapshot(ctx context.Context, image vm.Image) error
}

// ImageConfig controls image reconciliation.
type ImageConfig struct {
	// Logger receives image reconciliation errors.
	Logger              *slog.Logger
	OperationTimeout    time.Duration
	ImageMaximumIdle    time.Duration
	SnapshotMaximumIdle time.Duration
}

// ImageReconciler maintains controller-selected images on one host.
type ImageReconciler struct {
	passScheduler

	imageStore          ImageStore
	snapshotStore       SnapshotStore
	builder             MemorySnapshotBuilder
	operationTimeout    time.Duration
	imageMaximumIdle    time.Duration
	snapshotMaximumIdle time.Duration
	logger              *slog.Logger
}

// NewImageReconciler returns an image reconciler that runs at interval.
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
	if configuration.Logger == nil {
		configuration.Logger = slog.Default()
	}

	return &ImageReconciler{
		passScheduler:       newPassScheduler(interval),
		imageStore:          imageStore,
		snapshotStore:       snapshotStore,
		builder:             builder,
		operationTimeout:    configuration.OperationTimeout,
		imageMaximumIdle:    configuration.ImageMaximumIdle,
		snapshotMaximumIdle: configuration.SnapshotMaximumIdle,
		logger:              configuration.Logger,
	}
}

// Run maintains images until ctx is canceled.
func (r *ImageReconciler) Run(ctx context.Context) {
	r.run(ctx, r.reconcileAll)
}

// reconcileAll caches every image the controller selects, then prunes the rest.
func (r *ImageReconciler) reconcileAll(ctx context.Context) {
	listContext, cancelList := context.WithTimeout(ctx, r.operationTimeout)
	policies, err := r.imageStore.ImagePolicies(listContext)
	cancelList()

	if err != nil {
		r.logFailure(ctx, "load policies", err)
		return
	}

	for _, image := range policies {
		if image.CacheImage {
			r.reconcileImage(ctx, image)
		}
	}

	r.pruneLocalArtifacts(ctx, policies)
}

// reconcileImage caches one image and builds its warm artifact when requested.
// An image without a memory snapshot request loses its warm artifacts.
func (r *ImageReconciler) reconcileImage(ctx context.Context, image vm.Image) {
	operationContext, cancel := context.WithTimeout(ctx, r.operationTimeout)
	defer cancel()

	if err := r.imageStore.EnsureImage(operationContext, image); err != nil {
		r.logFailure(ctx, "cache image "+image.Name, err)
		return
	}

	if image.MemorySnapshot {
		if err := r.builder.EnsureMemorySnapshot(operationContext, image); err != nil {
			r.logFailure(ctx, "warm image "+image.Name, err)
		}
		return
	}

	// A warm disk with a VM disk cloned from it stays until that VM is gone.
	err := r.imageStore.RemoveWarmImages(operationContext, image.Name)
	if err != nil && !errors.Is(err, storage.ErrInUse) {
		r.logFailure(ctx, "remove warm image "+image.Name, err)
	}
}

// pruneLocalArtifacts removes images and staging that policies no longer keep.
// One timestamp covers both, and each artifact type uses its own idle window.
func (r *ImageReconciler) pruneLocalArtifacts(ctx context.Context, policies []vm.Image) {
	pruneContext, cancelPrune := context.WithTimeout(ctx, r.operationTimeout)
	defer cancelPrune()

	now := time.Now()
	err := r.imageStore.PruneImages(pruneContext, policies, now, r.imageMaximumIdle)
	if err == nil {
		err = r.snapshotStore.PruneStagedSnapshots(pruneContext, now, r.snapshotMaximumIdle)
	}
	if err != nil {
		r.logFailure(ctx, "prune local artifacts", err)
	}
}

// logFailure reports an error unless ctx ended, because a canceled pass fails
// every operation still in flight and those errors say nothing about the host.
func (r *ImageReconciler) logFailure(ctx context.Context, operation string, err error) {
	if ctx.Err() != nil {
		return
	}

	r.logger.Error("image reconciliation failed", "operation", operation, "error", err)
}

// Test change for the pull request stats comment.
