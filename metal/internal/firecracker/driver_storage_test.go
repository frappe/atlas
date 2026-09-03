package firecracker

import (
	"context"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeImages struct {
	releaseCalls int
	releaseError error
	staged       map[string]storage.StagedSnapshot
}

func (images *fakeImages) PrepareBoot(
	context.Context,
	storage.VirtualMachineStorageRequest,
) (storage.BootConfiguration, error) {
	return storage.BootConfiguration{}, nil
}

func (images *fakeImages) PrepareRootFileSystem(
	context.Context,
	storage.VirtualMachineStorageRequest,
) error {
	return nil
}

func (images *fakeImages) Release(context.Context, string) error {
	images.releaseCalls++
	return images.releaseError
}

func (images *fakeImages) ResizeDisk(context.Context, string, int) error {
	return nil
}

func (images *fakeImages) DiskUsage(context.Context, string) (storage.Usage, error) {
	return storage.Usage{}, nil
}

func (images *fakeImages) GetStagedSnapshot(
	snapshotID string,
) (storage.StagedSnapshot, bool, error) {
	snapshot, found := images.staged[snapshotID]
	return snapshot, found, nil
}

func (images *fakeImages) StageSnapshot(
	_ context.Context,
	virtualMachineID string,
	snapshotID string,
	_ string,
) (storage.StagedSnapshot, error) {
	if images.staged == nil {
		images.staged = make(map[string]storage.StagedSnapshot)
	}
	snapshot := storage.StagedSnapshot{ID: snapshotID, SourceVirtualMachineID: virtualMachineID}
	images.staged[snapshotID] = snapshot
	return snapshot, nil
}

func (images *fakeImages) EnsureImage(context.Context, vm.ImageRef) error {
	return nil
}

func (images *fakeImages) WarmImage(
	context.Context,
	vm.ImageRef,
	vm.MemorySnapshotConfiguration,
	string,
) (storage.WarmImageArtifacts, bool, error) {
	return storage.WarmImageArtifacts{}, false, nil
}

func (images *fakeImages) CreateWarmSourceSnapshot(context.Context, string, string) error {
	return nil
}

func (images *fakeImages) DeleteWarmSourceSnapshot(context.Context, string, string) error {
	return nil
}

func (images *fakeImages) PromoteWarmSnapshot(
	context.Context,
	storage.WarmImagePromotion,
) (storage.WarmImageArtifacts, error) {
	return storage.WarmImageArtifacts{}, nil
}

func (images *fakeImages) RemoveOtherWarmImages(context.Context, string, string) error {
	return nil
}

func (images *fakeImages) RecordImageUse(string, time.Time) error {
	return nil
}

func TestPauseStoppedVirtualMachineReturnsConflict(t *testing.T) {
	units := &stubUnits{}
	units.shutdown()
	machine := &machine{
		d:   &Driver{units: units, operationLocks: operationLocks{}},
		cfg: vmConfig{ID: "vm-1"},
	}

	if err := machine.Pause(context.Background()); err != vm.ErrConflict {
		t.Fatalf("pause stopped virtual machine = %v, want conflict", err)
	}
}
