package reconciler

import (
	"context"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeImageStore struct {
	policies            []vm.ImageRef
	cached              []string
	prunedImages        bool
	prunedSnapshots     bool
	snapshotMaximumIdle time.Duration
}

func (store *fakeImageStore) ImagePolicies(context.Context) ([]vm.ImageRef, error) {
	return store.policies, nil
}

func (store *fakeImageStore) EnsureImage(_ context.Context, image vm.ImageRef) error {
	store.cached = append(store.cached, image.Name)
	return nil
}

func (store *fakeImageStore) PruneImages(
	context.Context,
	[]vm.ImageRef,
	time.Time,
	time.Duration,
) error {
	store.prunedImages = true
	return nil
}

func (store *fakeImageStore) PruneStagedSnapshots(
	_ context.Context,
	_ time.Time,
	maximumIdle time.Duration,
) error {
	store.prunedSnapshots = true
	store.snapshotMaximumIdle = maximumIdle
	return nil
}

type fakeMemorySnapshotBuilder struct {
	images []string
}

func (builder *fakeMemorySnapshotBuilder) EnsureMemorySnapshot(
	_ context.Context,
	image vm.ImageRef,
) error {
	builder.images = append(builder.images, image.Name)
	return nil
}

func TestImageReconcilerCachesAndWarmsDesiredImages(t *testing.T) {
	store := &fakeImageStore{policies: []vm.ImageRef{
		{Name: "cold", CacheImage: true},
		{Name: "warm", CacheImage: true, MemorySnapshot: true},
		{Name: "on-demand"},
	}}
	builder := &fakeMemorySnapshotBuilder{}
	reconciler := NewImageReconciler(store, store, builder, time.Hour, ImageConfig{})

	reconciler.reconcileAll(context.Background())

	if len(store.cached) != 2 || store.cached[0] != "cold" || store.cached[1] != "warm" {
		t.Fatalf("cached images = %v", store.cached)
	}
	if len(builder.images) != 1 || builder.images[0] != "warm" {
		t.Fatalf("warm images = %v", builder.images)
	}
	if !store.prunedImages {
		t.Fatal("unused images were not pruned")
	}
	if !store.prunedSnapshots {
		t.Fatal("idle snapshots were not pruned")
	}
	if store.snapshotMaximumIdle != 48*time.Hour {
		t.Fatalf("snapshot maximum idle = %s", store.snapshotMaximumIdle)
	}
}
