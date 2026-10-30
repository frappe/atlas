package vm

import (
	"context"
	"errors"
	"testing"
)

func testRescueImage() Image {
	return Image{Name: "rescue", Architecture: testSpecification().Image.Architecture}
}

func newRescueManager(t *testing.T) (*Manager, *fakeRuntime) {
	t.Helper()
	manager, runtime, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	return manager, runtime
}

func applyRestart(t *testing.T, manager *Manager) {
	t.Helper()
	if err := manager.RequestRestart(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
}

func isInRescueMode(t *testing.T, manager *Manager) bool {
	t.Helper()
	observed, err := manager.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	return observed.IsInRescueMode
}

func TestRescueModeChangesOnlyAtTheNextRestart(t *testing.T) {
	manager, runtime := newRescueManager(t)
	ctx := context.Background()

	if err := manager.EnableRescueMode(ctx, "vm-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 1 || isInRescueMode(t, manager) {
		t.Fatalf("enable booted the VM: starts=%d", runtime.starts)
	}
	information, err := manager.Information(ctx, "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if !information.IsRescueModeEnabled {
		t.Fatal("enable did not report the pending rescue boot")
	}
	applyRestart(t, manager)
	if runtime.starts != 2 || !isInRescueMode(t, manager) {
		t.Fatalf("restart did not enter rescue mode: starts=%d", runtime.starts)
	}

	if err := manager.DisableRescueMode(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 2 || !isInRescueMode(t, manager) {
		t.Fatalf("disable booted the VM: starts=%d", runtime.starts)
	}
	applyRestart(t, manager)
	if runtime.starts != 3 || isInRescueMode(t, manager) {
		t.Fatalf("restart did not leave rescue mode: starts=%d", runtime.starts)
	}
}

func TestRescueModeRejectsChangesAndAllowsItsExits(t *testing.T) {
	manager, _ := newRescueManager(t)
	ctx := context.Background()
	if err := manager.EnableRescueMode(ctx, "vm-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	applyRestart(t, manager)
	specification := testSpecification()
	compute := Compute{CPUMillicores: specification.CPUMillicores, MemoryMiB: specification.MemoryMiB}

	rejected := map[string]func() error{
		"pause":   func() error { return manager.SetPowerState(ctx, "vm-1", StatePaused) },
		"compute": func() error { return manager.SetCompute(ctx, "vm-1", compute) },
		"resize":  func() error { return manager.Resize(ctx, "vm-1", compute, specification.DiskMiB) },
		"network": func() error { return manager.SetNetwork(ctx, "vm-1", NetworkConfiguration{}) },
		"ssh keys": func() error {
			_, err := manager.ReplaceSSHKeys(ctx, "vm-1", nil)
			return err
		},
		"metadata": func() error {
			_, err := manager.ReplaceMetadata(ctx, "vm-1", nil)
			return err
		},
		"sleep state": func() error { return manager.OffloadSleepState(ctx, "vm-1", SleepStateRequest{}) },
	}
	for name, change := range rejected {
		if err := change(); !errors.Is(err, ErrRescueMode) {
			t.Errorf("%s error = %v, want ErrRescueMode", name, err)
		}
	}

	allowed := map[string]func() error{
		"disk":    func() error { return manager.SetDisk(ctx, "vm-1", specification.DiskMiB+1024, specification.Disk) },
		"restart": func() error { return manager.RequestRestart(ctx, "vm-1") },
		"stop":    func() error { return manager.SetPowerState(ctx, "vm-1", StateStopped) },
		"start":   func() error { return manager.SetPowerState(ctx, "vm-1", StateRunning) },
		"disable": func() error { return manager.DisableRescueMode(ctx, "vm-1") },
		"delete":  func() error { return manager.Delete(ctx, "vm-1") },
	}
	for _, name := range []string{"disk", "restart", "stop", "start", "disable", "delete"} {
		if err := allowed[name](); err != nil {
			t.Errorf("%s error = %v", name, err)
		}
	}
}

type recordingSnapshots struct {
	fakeSnapshots
	request SnapshotRequest
}

func (snapshots *recordingSnapshots) Stage(_ context.Context, request SnapshotRequest) (StagedSnapshot, error) {
	snapshots.request = request
	return StagedSnapshot{}, nil
}

func TestRescueModeSnapshotCapturesTheVirtualMachineDisk(t *testing.T) {
	manager, _ := newRescueManager(t)
	snapshots := &recordingSnapshots{}
	manager.snapshots = snapshots
	image := testRescueImage()
	image.KernelSHA256 = "rescue-kernel"
	if err := manager.EnableRescueMode(t.Context(), "vm-1", image); err != nil {
		t.Fatal(err)
	}
	applyRestart(t, manager)

	if _, err := manager.CreateSnapshot(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	want := SnapshotRequest{VirtualMachineID: "vm-1", KernelSHA256: testSpecification().Image.KernelSHA256}
	if snapshots.request != want {
		t.Fatalf("snapshot request = %+v, want %+v", snapshots.request, want)
	}
}

func TestEnablingRescueModeWakesASleepingVirtualMachine(t *testing.T) {
	manager, runtime, _, _ := sleepyManager(t)
	if err := manager.EnableRescueMode(t.Context(), "vm-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	observed, err := manager.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if runtime.restores != 1 || observed.SleepState != SleepStateAwake {
		t.Fatalf("restores=%d sleep state=%q", runtime.restores, observed.SleepState)
	}
}

func TestEnablingRescueModeNeedsADiskUnlessAComaRestoresIt(t *testing.T) {
	manager, _, _, storage := newTestManager(t)
	if _, err := manager.Create(t.Context(), "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	storage.hasDisk = false
	if err := manager.EnableRescueMode(t.Context(), "vm-1", testRescueImage()); !errors.Is(err, ErrConflict) {
		t.Fatalf("no disk error = %v, want ErrConflict", err)
	}

	observed, err := manager.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	observed.SleepState = SleepStateComa
	if err := manager.store.writeObserved("vm-1", observed); err != nil {
		t.Fatal(err)
	}
	if err := manager.EnableRescueMode(t.Context(), "vm-1", testRescueImage()); err != nil {
		t.Fatalf("coma error = %v", err)
	}
}

func TestRescueImageHidesUserData(t *testing.T) {
	specification := testSpecification()
	specification.UserData = "#cloud-config"
	specification.RescueImage = &Image{Name: "rescue"}
	latest := specification.MetadataServiceData("vm-1", "10.0.0.1", "02:00:00:00:00:01")["latest"].(map[string]any)
	if _, found := latest["user-data"]; found {
		t.Fatal("rescue boot received user data")
	}
}
