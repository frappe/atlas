package vm

import (
	"context"
	"fmt"
	"testing"

	"github.com/frappe/atlas/metal/internal/network/traffic"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type testWarmImageStore struct {
	promotion *WarmImagePromotion
	removeErr error
}

func (store *testWarmImageStore) FindWarmImage(context.Context, Image, MemorySnapshotConfiguration, string) (bool, error) {
	return false, nil
}

func (store *testWarmImageStore) PromoteWarmImage(_ context.Context, promotion WarmImagePromotion) error {
	store.promotion = &promotion
	return nil
}

func (store *testWarmImageStore) RemoveOtherWarmImages(context.Context, string, string) error {
	return store.removeErr
}

type testWarmRuntime struct {
	*fakeRuntime
}

func (runtime *testWarmRuntime) Compatibility() string {
	return "firecracker-test"
}

func (runtime *testWarmRuntime) CreateMemorySnapshot(context.Context, RuntimeMachine) (string, string, error) {
	return "/state", "/memory", nil
}

func TestWarmImageBuilderUsesManagerOwnedTemporaryMachine(t *testing.T) {
	baseRuntime := &fakeRuntime{state: StateStopped}
	runtime := &testWarmRuntime{fakeRuntime: baseRuntime}
	manager, err := NewManager(
		ManagerConfig{MachinesDirectory: t.TempDir()},
		ManagerDependencies{Runtime: runtime, Network: &fakeNetwork{}, Storage: &fakeStorage{}, Snapshots: fakeSnapshots{}, Traffic: &traffic.Monitor{}, Telemetry: telemetry.NewRegistry(nil)},
	)
	if err != nil {
		t.Fatal(err)
	}
	store := &testWarmImageStore{}
	builder := NewWarmImageBuilder(manager, runtime, store)
	builder.warmupDelay = 0
	image := testSpecification().Image
	image.CacheImage = true
	image.MemorySnapshot = true
	image.MemorySnapshotConfiguration = &MemorySnapshotConfiguration{VirtualCPUCount: 1, MemoryMiB: 256, DiskMiB: 1024}

	if err := builder.EnsureMemorySnapshot(t.Context(), image); err != nil {
		t.Fatal(err)
	}
	if store.promotion == nil || store.promotion.SourceVirtualMachineID == "" {
		t.Fatal("warm image was not promoted")
	}
	if baseRuntime.starts != 1 || baseRuntime.pauses != 1 || baseRuntime.removes != 1 {
		t.Fatalf("runtime calls = start %d, pause %d, remove %d", baseRuntime.starts, baseRuntime.pauses, baseRuntime.removes)
	}
}

func TestWarmImageBuilderBuildsWhileOldWarmDiskIsInUse(t *testing.T) {
	baseRuntime := &fakeRuntime{state: StateStopped}
	runtime := &testWarmRuntime{fakeRuntime: baseRuntime}
	manager, err := NewManager(
		ManagerConfig{MachinesDirectory: t.TempDir()},
		ManagerDependencies{Runtime: runtime, Network: &fakeNetwork{}, Storage: &fakeStorage{}, Snapshots: fakeSnapshots{}, Traffic: &traffic.Monitor{}, Telemetry: telemetry.NewRegistry(nil)},
	)
	if err != nil {
		t.Fatal(err)
	}
	store := &testWarmImageStore{removeErr: fmt.Errorf("remove old warm disk: %w", ErrInUse)}
	builder := NewWarmImageBuilder(manager, runtime, store)
	builder.warmupDelay = 0
	image := testSpecification().Image
	image.CacheImage = true
	image.MemorySnapshot = true
	image.MemorySnapshotConfiguration = &MemorySnapshotConfiguration{VirtualCPUCount: 1, MemoryMiB: 256, DiskMiB: 1024}

	if err := builder.EnsureMemorySnapshot(t.Context(), image); err != nil {
		t.Fatal(err)
	}
	if store.promotion == nil {
		t.Fatal("new warm image was not promoted")
	}
	if baseRuntime.starts != 1 {
		t.Fatalf("temporary VM starts = %d, want 1", baseRuntime.starts)
	}
}
