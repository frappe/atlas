package reconciler

import (
	"context"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type fakeImageStore struct {
	policies            []vm.Image
	cached              []string
	removedWarmImages   []string
	prunedImages        bool
	usedKernels         []string
	prunedSnapshots     bool
	snapshotMaximumIdle time.Duration
	imageMaximumIdle    time.Duration
}

func (store *fakeImageStore) ImagePolicies(context.Context) ([]vm.Image, error) {
	return store.policies, nil
}

func (store *fakeImageStore) EnsureImage(_ context.Context, image vm.Image) error {
	store.cached = append(store.cached, image.Name)
	return nil
}

func (store *fakeImageStore) RemoveWarmImages(_ context.Context, imageReference string) error {
	store.removedWarmImages = append(store.removedWarmImages, imageReference)
	return nil
}

func (store *fakeImageStore) PruneImages(
	_ context.Context,
	_ []vm.Image,
	_ time.Time,
	maximumIdle time.Duration,
) error {
	store.prunedImages = true
	store.imageMaximumIdle = maximumIdle
	return nil
}

func (store *fakeImageStore) PruneKernels(usedKernelSHA256s []string, _ time.Time, _ time.Duration) error {
	store.usedKernels = usedKernelSHA256s
	return nil
}

type fakeVirtualMachines []vm.Information

func (virtualMachines fakeVirtualMachines) List(context.Context) ([]vm.Information, error) {
	return virtualMachines, nil
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
	image vm.Image,
) error {
	builder.images = append(builder.images, image.Name)
	return nil
}

func TestImageReconcilerCachesAndWarmsDesiredImages(t *testing.T) {
	store := &fakeImageStore{policies: []vm.Image{
		{Name: "cold", CacheImage: true},
		{Name: "warm", CacheImage: true, MemorySnapshot: true},
		{Name: "on-demand"},
	}}
	builder := &fakeMemorySnapshotBuilder{}
	virtualMachines := fakeVirtualMachines{{ID: "vm-1", Image: vm.Image{KernelSHA256: "kernel"}}}
	reconciler := NewImageReconciler(store, store, builder, virtualMachines, time.Hour, ImageConfig{Telemetry: telemetry.NewRegistry(nil)})

	reconciler.reconcileAll(context.Background())

	if len(store.cached) != 2 || store.cached[0] != "cold" || store.cached[1] != "warm" {
		t.Fatalf("cached images = %v", store.cached)
	}
	if len(builder.images) != 1 || builder.images[0] != "warm" {
		t.Fatalf("warm images = %v", builder.images)
	}
	if len(store.removedWarmImages) != 1 || store.removedWarmImages[0] != "cold" {
		t.Fatalf("removed warm images = %v", store.removedWarmImages)
	}
	if !store.prunedImages {
		t.Fatal("unused images were not pruned")
	}
	if len(store.usedKernels) != 1 || store.usedKernels[0] != "kernel" {
		t.Fatalf("used kernels = %v", store.usedKernels)
	}
	if !store.prunedSnapshots {
		t.Fatal("idle snapshots were not pruned")
	}
	if store.imageMaximumIdle != time.Hour {
		t.Fatalf("image maximum idle = %s", store.imageMaximumIdle)
	}
	if store.snapshotMaximumIdle != 48*time.Hour {
		t.Fatalf("snapshot maximum idle = %s", store.snapshotMaximumIdle)
	}
}
