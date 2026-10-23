package vm

import (
	"context"
	"errors"
	"testing"
)

func sleepRestoreManager(t *testing.T, state SleepState) (*Manager, *fakeRuntime, *fakeStorage, *sleepSnapshots) {
	t.Helper()
	manager, runtime, storage, snapshots := sleepyManager(t)
	if state != SleepStateSleeping {
		request := SleepStateRequest{Memory: sleepTarget()}
		if state == SleepStateComa {
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

func TestRepeatedSleepRestoreFailuresColdBootTheExistingDisk(t *testing.T) {
	for _, state := range []SleepState{SleepStateSleeping, SleepStateDeepSleeping, SleepStateComa} {
		for _, resultState := range []State{StateRunning, StatePaused} {
			t.Run(string(state)+"/"+string(resultState), func(t *testing.T) {
				manager, runtime, storage, _ := sleepRestoreManager(t, state)
				releases := storage.releases
				runtime.restoreError = errors.New("snapshot load failed")
				if err := manager.SetPowerState(t.Context(), "vm-1", resultState); err != nil {
					t.Fatal(err)
				}
				for range maximumRestoreAttempts - 1 {
					if err := reconcileSleeping(t.Context(), manager, "vm-1"); err == nil {
						t.Fatal("a failed restore reported success")
					}
					if runtime.coldStarts != 0 || !runtime.hasSavedState {
						t.Fatalf("one failed restore discarded the saved state: %+v", runtime)
					}
				}
				if err := reconcileSleeping(t.Context(), manager, "vm-1"); err != nil {
					t.Fatal(err)
				}
				if err := manager.Reconcile(t.Context(), "vm-1"); err != nil {
					t.Fatal(err)
				}
				information, err := manager.Information(t.Context(), "vm-1")
				if err != nil || information.State != resultState || information.SleepState != SleepStateAwake || information.Error != nil {
					t.Fatalf("fallback did not complete: %+v, %v", information, err)
				}
				if runtime.coldStarts != 1 || runtime.removes != 1 || runtime.hasSavedState || runtime.restores+runtime.restoresPaused != maximumRestoreAttempts || storage.releases != releases {
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

func TestSleepRestoreFallbackStopsOnCancellationOrAMissingDisk(t *testing.T) {
	for _, failure := range []string{"canceled", "deadline", "disk", "download"} {
		t.Run(failure, func(t *testing.T) {
			manager, runtime, storage, snapshots := sleepRestoreManager(t, SleepStateComa)
			runtime.restoreError = errors.New("snapshot load failed")
			switch failure {
			case "canceled":
				runtime.restoreError = context.Canceled
			case "deadline":
				runtime.restoreError = context.DeadlineExceeded
			case "disk":
				storage.hasDisk = false
			case "download":
				snapshots.downloadError = errors.New("expired URL")
			}
			if err := wakeTestSleep(t.Context(), manager); err == nil {
				t.Fatal("wake failure was hidden")
			}
			information, err := manager.Information(t.Context(), "vm-1")
			if err != nil || information.Error == nil || information.SleepState != SleepStateComa || runtime.coldStarts != 0 || runtime.removes != 0 {
				t.Fatalf("failed wake discarded memory or booted: %+v, %v; runtime=%+v", information, err, runtime)
			}
		})
	}
}

func TestSleepRestoreFallbackRetryDoesNotReloadRemoteMemory(t *testing.T) {
	for _, failure := range []string{"cleanup", "boot"} {
		t.Run(failure, func(t *testing.T) {
			manager, runtime, storage, snapshots := sleepRestoreManager(t, SleepStateComa)
			runtime.restoreError = errors.New("snapshot load failed")
			if failure == "cleanup" {
				runtime.deleteError = errors.New("saved state cleanup failed")
			} else {
				runtime.coldStartError = errors.New("cold boot failed")
			}
			if err := manager.SetPowerState(t.Context(), "vm-1", StateRunning); err != nil {
				t.Fatal(err)
			}
			for range maximumRestoreAttempts {
				if err := reconcileSleeping(t.Context(), manager, "vm-1"); err == nil {
					t.Fatal("restore or fallback failure was hidden")
				}
			}
			information, err := manager.Information(t.Context(), "vm-1")
			if err != nil || information.Error == nil || information.SleepState != SleepStateAwake {
				t.Fatalf("failed fallback kept remote memory eligible: %+v, %v", information, err)
			}
			downloads := len(snapshots.downloads)
			runtime.deleteError, runtime.coldStartError, runtime.restoreError = nil, nil, nil
			restarted, err := NewManager(manager.configuration, ManagerDependencies{Runtime: runtime, Network: manager.network,
				Storage: storage, Snapshots: snapshots, Telemetry: manager.telemetry})
			if err != nil {
				t.Fatal(err)
			}
			if err := restarted.Reconcile(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			wantRestores := maximumRestoreAttempts
			if failure == "cleanup" {
				wantRestores++
			}
			if runtime.state != StateRunning || runtime.restores != wantRestores || len(snapshots.downloads) != downloads {
				t.Fatalf("retry restored stale memory: runtime=%+v downloads=%v", runtime, snapshots.downloads)
			}
		})
	}
}

func TestANewSaveOfAPausedGuestReplacesTheRemoteCheckpoint(t *testing.T) {
	manager, runtime, _, snapshots := sleepRestoreManager(t, SleepStateDeepSleeping)
	// The guest resumed paused, but the record still shows the remote checkpoint.
	runtime.state, runtime.hasSavedState = StatePaused, false

	if err := reconcileSleeping(t.Context(), manager, "vm-1"); err != nil {
		t.Fatal(err)
	}

	information, err := manager.Information(t.Context(), "vm-1")
	if err != nil || information.SleepSnapshot != nil || information.Error != nil {
		t.Fatalf("the new local save kept the remote checkpoint: %+v, %v", information, err)
	}
	if runtime.saves != 1 || len(snapshots.downloads) != 0 {
		t.Fatalf("the guest resumed the older remote checkpoint: runtime=%+v downloads=%v", runtime, snapshots.downloads)
	}
}
