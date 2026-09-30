package vm

import (
	"context"
	"errors"
	"fmt"
	"testing"
)

func rescueMachine(t *testing.T) (*Manager, *fakeRuntime, *fakeStorage) {
	t.Helper()
	manager, runtime, _, storage := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	return manager, runtime, storage
}

func TestRescueStoppedSelectionAndExitPreservePower(t *testing.T) {
	manager, runtime, storage := rescueMachine(t)
	ctx := context.Background()
	if err := manager.SetPowerState(ctx, "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 1 || runtime.state != StateStopped || storage.rescueDiskReleases != 0 {
		t.Fatal("selecting rescue started the stopped VM or deleted its disk")
	}
	if err := manager.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	desired, observed, err := manager.newVirtualMachine("machine-1").records()
	if err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 1 || storage.rescueDiskReleases != 1 || observed.RescueGeneration != desired.RescueGeneration {
		t.Fatalf("stopped exit: starts=%d releases=%d desired=%d observed=%d", runtime.starts, storage.rescueDiskReleases, desired.RescueGeneration, observed.RescueGeneration)
	}
}

func TestRescueURLRefreshKeepsSessionAndDifferentImageConflicts(t *testing.T) {
	manager, runtime, _ := rescueMachine(t)
	ctx := context.Background()
	image := testRescueImage()
	if err := manager.RequestRescue(ctx, "machine-1", image); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	image.RootfsURL += "?renewed=true"
	if err := manager.RequestRescue(ctx, "machine-1", image); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	desired, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 2 || desired.RescueGeneration != 1 || desired.Specification.Rescue.Image.RootfsURL != image.RootfsURL {
		t.Fatal("URL refresh restarted rescue or lost the refreshed URL")
	}
	image.Name = "different-image"
	if err := manager.RequestRescue(ctx, "machine-1", image); !errors.Is(err, ErrConflict) {
		t.Fatalf("different image: %v", err)
	}
}

func TestRescueStopStartPreservesSessionAndRestartExits(t *testing.T) {
	manager, runtime, storage := rescueMachine(t)
	ctx := context.Background()
	if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	for _, state := range []State{StateRunning, StateStopped, StateRunning} {
		if err := manager.SetPowerState(ctx, "machine-1", state); err != nil {
			t.Fatal(err)
		}
		if err := manager.Reconcile(ctx, "machine-1"); err != nil {
			t.Fatal(err)
		}
	}
	desired, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if !desired.Specification.Rescue.Enabled || desired.RescueGeneration != 1 || storage.rescueDiskReleases != 0 {
		t.Fatal("Stop/Start ended the rescue session")
	}
	if err := manager.RequestRestart(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	storage.releaseRescueError = errors.New("cleanup failed")
	if err := manager.Reconcile(ctx, "machine-1"); err == nil {
		t.Fatal("cleanup failure was ignored")
	}
	if runtime.state != StateStopped {
		t.Fatal("cleanup ran before the guest stopped")
	}
	storage.releaseRescueError = nil
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	desired, err = manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if desired.Specification.Rescue.Enabled || runtime.state != StateRunning {
		t.Fatal("restart did not exit rescue")
	}
}

func TestRescueRequiresDiskAndBlocksResize(t *testing.T) {
	manager, _, storage := rescueMachine(t)
	ctx := context.Background()
	storage.missingDisk = true
	if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); !errors.Is(err, ErrConflict) {
		t.Fatalf("missing original disk: %v", err)
	}
	storage.missingDisk = false
	if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(ctx, "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	compute := Compute{CPUMillicores: 4000, MemoryMiB: 4096}
	for _, change := range []func() error{
		func() error { return manager.SetCompute(ctx, "machine-1", compute) },
		func() error { return manager.SetDisk(ctx, "machine-1", 8192, Disk{}) },
		func() error { return manager.Resize(ctx, "machine-1", compute, 8192) },
	} {
		if err := change(); !errors.Is(err, ErrConflict) {
			t.Fatalf("rescue resize: %v", err)
		}
	}
}

func TestRescueMetadataKeepsKeysAndSuppressesUserData(t *testing.T) {
	specification := testSpecification()
	specification.SSHKeys = []string{"ssh-ed25519 example"}
	specification.UserData = "original provisioning"
	specification.Rescue.Enabled = true
	latest := specification.MetadataServiceData("machine-1", "", "")["latest"].(map[string]any)
	if _, found := latest["user-data"]; found {
		t.Fatal("rescue contains original user data")
	}
	keys := latest["meta-data"].(map[string]any)["public-keys"].(map[string]any)
	if len(keys) != 1 {
		t.Fatal("rescue lost SSH keys")
	}
}

func TestRescueUnexpectedStopPreservesSessionAndReportedRebootExits(t *testing.T) {
	for _, reported := range []bool{false, true} {
		t.Run(fmt.Sprint(reported), func(t *testing.T) {
			manager, runtime, storage := rescueMachine(t)
			ctx := context.Background()
			if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); err != nil {
				t.Fatal(err)
			}
			if err := manager.Reconcile(ctx, "machine-1"); err != nil {
				t.Fatal(err)
			}
			runtime.state = StateStopped
			runtime.rescueRebootRequested = reported
			if err := manager.Reconcile(ctx, "machine-1"); err != nil {
				t.Fatal(err)
			}
			desired, err := manager.store.readDesired("machine-1")
			if err != nil {
				t.Fatal(err)
			}
			if desired.Specification.Rescue.Enabled == reported || runtime.state != StateRunning {
				t.Fatalf("reported=%v rescue=%v state=%v", reported, desired.Specification.Rescue.Enabled, runtime.state)
			}
			if (storage.rescueDiskReleases > 0) != reported {
				t.Fatal("unexpected rescue disk cleanup")
			}
		})
	}
}

func TestRescueBlocksSnapshotsUntilExitIsApplied(t *testing.T) {
	manager, _, _ := rescueMachine(t)
	ctx := context.Background()
	if err := manager.RequestRescue(ctx, "machine-1", testRescueImage()); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.CreateSnapshot(ctx, "machine-1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("rescue snapshot: %v", err)
	}
	if err := manager.RequestRescueExit(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.CreateSnapshot(ctx, "machine-1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("pending exit snapshot: %v", err)
	}
}
