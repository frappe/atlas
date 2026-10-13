package vm

import (
	"context"
	"errors"
	"testing"
	"time"
)

// setObservedState writes one VM's observed state for a migration test.
func setObservedState(t *testing.T, machines *Manager, virtualMachineID string, state State) {
	t.Helper()
	record := ObservedRecord{State: state, Generation: 1, UpdatedAt: time.Now().UTC()}
	if err := machines.store.writeObserved(virtualMachineID, record); err != nil {
		t.Fatal(err)
	}
}

func TestNormalizeSourceToStopped(t *testing.T) {
	cases := []struct {
		name         string
		state        State
		savedState   bool
		wantStops    int
		wantResumes  int
		wantRestores int
	}{
		{"running stops", StateRunning, false, 1, 0, 0},
		{"created stops", StateCreated, false, 1, 0, 0},
		{"paused resumes then stops", StatePaused, false, 1, 1, 0},
		{"saved state restores then stops", StateStopped, true, 1, 0, 1},
		{"stopped without saved state is a no-op", StateStopped, false, 0, 0, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			machines, runtime, _, _ := newTestManager(t)
			ctx := context.Background()
			if _, err := machines.Create(ctx, "vm-1", testSpecification()); err != nil {
				t.Fatal(err)
			}
			setObservedState(t, machines, "vm-1", tc.state)
			runtime.state = tc.state
			runtime.hasSavedState = tc.savedState

			if err := NewMigrationHost(machines).NormalizeSourceToStopped(ctx, "vm-1"); err != nil {
				t.Fatal(err)
			}
			if runtime.stops != tc.wantStops || runtime.resumes != tc.wantResumes || runtime.restores != tc.wantRestores {
				t.Fatalf("stops %d, resumes %d, restores %d", runtime.stops, runtime.resumes, runtime.restores)
			}
			if runtime.state != StateStopped {
				t.Fatalf("state = %s, want stopped", runtime.state)
			}
		})
	}
}

func TestNormalizeSourceToStoppedRefusesFailedState(t *testing.T) {
	machines, runtime, _, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := machines.Create(ctx, "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	setObservedState(t, machines, "vm-1", StateFailed)
	runtime.state = StateFailed

	if err := NewMigrationHost(machines).NormalizeSourceToStopped(ctx, "vm-1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("failed source = %v, want ErrConflict", err)
	}
}

// seedDestinationVM writes reconstructed destination records in the original state.
func seedDestinationVM(t *testing.T, machines *Manager, desiredState State) {
	t.Helper()
	desired := DesiredRecord{
		ID: "vm-1", UserID: 1000, GroupID: 1000, State: desiredState,
		CreateFingerprint:       "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		Specification:           testSpecification(),
		Generation:              2,
		SpecificationGeneration: 1,
		RestartGeneration:       1,
	}
	if err := machines.store.writeDesired(desired); err != nil {
		t.Fatal(err)
	}
	setObservedState(t, machines, "vm-1", StateUnknown)
}

func TestEnsureMigrationNetworkRecordsTheInterface(t *testing.T) {
	machines, _, network, _ := newTestManager(t)
	seedDestinationVM(t, machines, StateRunning)

	if err := NewMigrationHost(machines).EnsureMigrationNetwork(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if network.ensures != 1 {
		t.Fatalf("network ensures = %d, want 1", network.ensures)
	}
	observed, err := machines.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.NetworkInterface.MACAddress == "" {
		t.Fatal("network interface was not recorded")
	}
}

func TestApplyMigratedDestinationState(t *testing.T) {
	cases := []struct {
		name           string
		desiredState   State
		wantColdStarts int
		wantPauses     int
	}{
		{"running cold-starts", StateRunning, 1, 0},
		{"paused cold-starts then pauses", StatePaused, 1, 1},
		{"stopped keeps the runtime stopped", StateStopped, 0, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			machines, runtime, _, storage := newTestManager(t)
			runtime.state = StateStopped
			seedDestinationVM(t, machines, tc.desiredState)

			if err := NewMigrationHost(machines).ApplyMigratedDestinationState(context.Background(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			if runtime.coldStarts != tc.wantColdStarts || runtime.pauses != tc.wantPauses {
				t.Fatalf("cold starts = %d, pauses = %d", runtime.coldStarts, runtime.pauses)
			}
			// A resize migration grows the received disk before start.
			if storage.resizes != 1 {
				t.Fatalf("disk resizes = %d, want 1", storage.resizes)
			}
			observed, err := machines.store.readObserved("vm-1")
			if err != nil {
				t.Fatal(err)
			}
			if observed.State != tc.desiredState || observed.Generation != 2 {
				t.Fatalf("observed = %s at generation %d", observed.State, observed.Generation)
			}
		})
	}
}

func TestApplyMigratedDestinationStateReportsAStartFailure(t *testing.T) {
	machines, runtime, _, _ := newTestManager(t)
	runtime.state = StateStopped
	runtime.coldStartError = errors.New("boot failed")
	seedDestinationVM(t, machines, StateRunning)

	if err := NewMigrationHost(machines).ApplyMigratedDestinationState(context.Background(), "vm-1"); err == nil {
		t.Fatal("want a cold-start failure")
	}
	observed, err := machines.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.State == StateRunning {
		t.Fatal("a failed start must not record running")
	}
}

func TestLimitSourceDiskAppliesAndClampsTheThrottle(t *testing.T) {
	machines, runtime, _, _ := newTestManager(t)
	ctx := context.Background()
	spec := testSpecification()
	spec.Disk.ThroughputMiBps = 100 // configured limit
	if _, err := machines.Create(ctx, "vm-1", spec); err != nil {
		t.Fatal(err)
	}
	setObservedState(t, machines, "vm-1", StateRunning)
	runtime.state = StateRunning

	// A throttle below the configured limit applies as requested.
	applied, err := NewMigrationHost(machines).LimitSourceDisk(ctx, "vm-1", 64)
	if err != nil {
		t.Fatal(err)
	}
	if applied != 64 || runtime.diskLimitMiBps != 64 {
		t.Fatalf("throttle = %d, runtime saw %d, want 64", applied, runtime.diskLimitMiBps)
	}

	// A throttle above the configured limit clamps down to it.
	applied, err = NewMigrationHost(machines).LimitSourceDisk(ctx, "vm-1", 500)
	if err != nil {
		t.Fatal(err)
	}
	if applied != 100 || runtime.diskLimitMiBps != 100 {
		t.Fatalf("clamped throttle = %d, runtime saw %d, want 100", applied, runtime.diskLimitMiBps)
	}

	// A nonpositive throttle is a no-op and touches nothing on the guest.
	runtime.diskRefresh = 0
	applied, err = NewMigrationHost(machines).LimitSourceDisk(ctx, "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	if applied != 0 || runtime.diskRefresh != 0 {
		t.Fatalf("nonpositive throttle = %d, refreshes = %d, want 0 and 0", applied, runtime.diskRefresh)
	}
}

func TestRemoveMigrationNetworkReleases(t *testing.T) {
	machines, _, network, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := machines.Create(ctx, "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}

	if err := NewMigrationHost(machines).RemoveMigrationNetwork(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if network.releases != 1 {
		t.Fatalf("network releases = %d, want 1", network.releases)
	}
}

func TestMigrationMemorySaveAndRollback(t *testing.T) {
	for _, state := range []State{StateRunning, StatePaused, StateStopped} {
		t.Run(string(state), func(t *testing.T) {
			machines, runtime, _, _ := newTestManager(t)
			seedDestinationVM(t, machines, state)
			runtime.state = state
			host := NewMigrationHost(machines)
			if err := host.SaveSourceMemory(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			if runtime.saves != 1 || runtime.stops != 0 || !runtime.hasSavedState {
				t.Fatal("source discarded guest memory")
			}
			if err := host.RestoreRuntimeState(t.Context(), "vm-1", state, true); err != nil {
				t.Fatal(err)
			}
			if runtime.state != state || runtime.coldStarts != 0 {
				t.Fatal("rollback cold booted the source")
			}
			if err := host.RestoreRuntimeState(t.Context(), "vm-1", state, true); err != nil {
				t.Fatal(err)
			}
			if runtime.restores+runtime.restoresPaused > 1 {
				t.Fatal("rollback restored twice")
			}
		})
	}
}

func TestMemoryDestinationLoadsPausedThenActivates(t *testing.T) {
	for _, desiredState := range []State{StateRunning, StatePaused, StateStopped} {
		t.Run(string(desiredState), func(t *testing.T) {
			machines, runtime, _, _ := newTestManager(t)
			seedDestinationVM(t, machines, desiredState)
			runtime.state = StateStopped
			host := NewMigrationHost(machines)
			for range 2 {
				if err := host.RestoreMigratedMemory(t.Context(), "vm-1", "/incoming/bundle"); err != nil {
					t.Fatal(err)
				}
			}
			if runtime.state != StatePaused || runtime.migrationRestores != 1 || runtime.resumes != 0 || runtime.coldStarts != 0 {
				t.Fatalf("runtime after restore = %+v", runtime)
			}
			if runtime.migrationDirectory != "/incoming/bundle" || runtime.metadata != 2 || runtime.diskRefresh != 2 {
				t.Fatal("restore did not apply the incoming bundle and destination configuration")
			}
			for range 2 {
				if err := host.ActivateMigratedMemory(t.Context(), "vm-1"); err != nil {
					t.Fatal(err)
				}
			}
			wantResumes := 0
			if desiredState == StateRunning {
				wantResumes = 1
			}
			if runtime.state != desiredState || runtime.resumes != wantResumes || runtime.migrationRestores != 1 || runtime.coldStarts != 0 {
				t.Fatalf("runtime after activation = %+v", runtime)
			}
			wantGuestChecks := 0
			if desiredState == StateRunning {
				wantGuestChecks = 2
			}
			if runtime.guestChecks != wantGuestChecks {
				t.Fatalf("guest checks = %d, want %d", runtime.guestChecks, wantGuestChecks)
			}
		})
	}
}

func TestMemoryActivationRetainsRunningGuestAfterHealthCheckFailure(t *testing.T) {
	machines, runtime, _, _ := newTestManager(t)
	seedDestinationVM(t, machines, StateRunning)
	runtime.state = StatePaused
	failure := errors.New("guest SSH unavailable")
	runtime.guestCheckError = failure
	host := NewMigrationHost(machines)
	if err := host.ActivateMigratedMemory(t.Context(), "vm-1"); !errors.Is(err, failure) {
		t.Fatalf("activation error = %v", err)
	}
	if runtime.state != StateRunning || runtime.resumes != 1 || runtime.migrationRestores != 0 {
		t.Fatalf("runtime after failed check = %+v", runtime)
	}
	runtime.guestCheckError = nil
	if err := host.ActivateMigratedMemory(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.guestChecks != 2 || runtime.resumes != 1 || runtime.migrationRestores != 0 {
		t.Fatal("health check retry replayed guest memory")
	}
}

func TestMemoryDestinationDoesNotHideRestoreFailures(t *testing.T) {
	for _, failing := range []string{"load", "metadata", "disk", "already_running"} {
		t.Run(failing, func(t *testing.T) {
			machines, runtime, _, _ := newTestManager(t)
			seedDestinationVM(t, machines, StateRunning)
			runtime.state = StateStopped
			failure := errors.New("restore interrupted")
			switch failing {
			case "load":
				runtime.restoreError = failure
			case "metadata":
				runtime.metadataError = failure
			case "disk":
				runtime.diskError = failure
			case "already_running":
				runtime.state = StateRunning
			}
			err := NewMigrationHost(machines).RestoreMigratedMemory(t.Context(), "vm-1", "/incoming/bundle")
			if err == nil || runtime.coldStarts != 0 || runtime.resumes != 0 {
				t.Fatalf("error = %v, runtime = %+v", err, runtime)
			}
			if failing != "already_running" && !errors.Is(err, failure) {
				t.Fatal(err)
			}
		})
	}
	machines, runtime, _, _ := newTestManager(t)
	seedDestinationVM(t, machines, StateRunning)
	runtime.state = StateStopped
	if err := NewMigrationHost(machines).ActivateMigratedMemory(t.Context(), "vm-1"); err == nil || runtime.migrationRestores != 0 || runtime.coldStarts != 0 {
		t.Fatal("activation replayed old memory after losing the destination process")
	}
}
