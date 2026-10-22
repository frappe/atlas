package vm

import (
	"errors"
	"testing"
)

func sleepRestoreManager(t *testing.T, state SleepState) (*Manager, *fakeRuntime, *fakeStorage, *sleepSnapshots) {
	t.Helper()
	manager, runtime, storage, snapshots := sleepyManager(t)
	if state != SleepStateSleeping {
		request := SleepStateRequest{Memory: sleepTarget()}
		if state == SleepStateComatose {
			request.Disk = sleepTarget()
		}
		if err := manager.OffloadSleepState(t.Context(), "vm-1", request); err != nil {
			t.Fatal(err)
		}
		if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
			t.Fatal(err)
		}
	}
	// The snapshot fake restores the disk; HasDisk reads the separate storage fake.
	storage.hasDisk = true
	return manager, runtime, storage, snapshots
}

func TestSleepRestoreFailureColdBootsTheExistingDisk(t *testing.T) {
	for _, state := range []SleepState{SleepStateSleeping, SleepStateDeepSleeping, SleepStateComatose} {
		for _, resultState := range []State{StateRunning, StatePaused} {
			t.Run(string(state)+"/"+string(resultState), func(t *testing.T) {
				manager, runtime, storage, _ := sleepRestoreManager(t, state)
				releases := storage.releases
				runtime.restoreError = errors.New("snapshot load failed")
				if err := manager.SetPowerState(t.Context(), "vm-1", resultState); err != nil {
					t.Fatal(err)
				}
				if err := manager.RestoreSleepState(t.Context(), "vm-1"); err != nil {
					t.Fatal(err)
				}
				if err := manager.Reconcile(t.Context(), "vm-1"); err != nil {
					t.Fatal(err)
				}
				information, err := manager.Information(t.Context(), "vm-1")
				if err != nil || information.State != resultState || information.SleepState != SleepStateAwake || information.Error != nil {
					t.Fatalf("fallback did not complete: %+v, %v", information, err)
				}
				if runtime.coldStarts != 1 || runtime.removes != 1 || runtime.hasSavedState || runtime.restores+runtime.restoresPaused != 1 || storage.releases != releases {
					t.Fatalf("fallback repeated, retained memory, or released the disk: runtime=%+v storage=%+v", runtime, storage)
				}
			})
		}
	}
}

func TestSleepRestoreKeepsAGuestThatResumedDespiteAnError(t *testing.T) {
	manager, runtime, _, _ := sleepRestoreManager(t, SleepStateDeepSleeping)
	runtime.restoreError = errors.New("saved state cleanup failed")
	runtime.restoreFailureState = StateRunning
	if err := wakeTestSleep(t.Context(), manager); err != nil {
		t.Fatal(err)
	}
	if runtime.coldStarts != 0 || runtime.removes != 0 || runtime.state != StateRunning {
		t.Fatalf("fallback replaced a resumed guest: %+v", runtime)
	}
}
