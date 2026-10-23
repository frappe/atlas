package migration

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func sleepMigration(t *testing.T, state vm.SleepState) *vm.SleepMigration {
	t.Helper()
	data := fmt.Sprintf(`{"state":%q,"checkpoint":{"id":"checkpoint-1","identity":%q,"source_user_id":1000,"specification_generation":1,
		"memory":{"object_key":"sleep/source/vm-1/memory","download_url":"https://objects.example/memory?get=secret","artifact":{"size_bytes":512,"stored_size_bytes":128,"sha256":%q}},
		"disk":{"object_key":"sleep/source/vm-1/disk","download_url":"https://objects.example/disk?get=secret","artifact":{"size_bytes":1024,"stored_size_bytes":256,"sha256":%q}}}}`, state, strings.Repeat("c", 64), strings.Repeat("a", 64), strings.Repeat("b", 64))
	var sleep vm.SleepMigration
	if err := json.Unmarshal([]byte(data), &sleep); err != nil {
		t.Fatal(err)
	}
	return &sleep
}

func TestOffloadedSourceNeverRestoresDuringCutoverOrAbort(t *testing.T) {
	for _, state := range []vm.SleepState{vm.SleepStateDeepSleeping, vm.SleepStateComa} {
		t.Run(string(state), func(t *testing.T) {
			manager, host, _ := newMigrationManager(t)
			host.create("vm-1", testSpecification())
			host.observed["vm-1"] = vm.ObservedRecord{Generation: 1, State: vm.StateStopped, SleepState: state}
			host.sleep = sleepMigration(t, state)
			if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1", false); !errors.Is(err, vm.ErrConflict) {
				t.Fatalf("an older destination locked offloaded state: %v", err)
			}
			for range 2 {
				description, err := manager.LockSource(t.Context(), "mig-1", "vm-1", true)
				if err != nil || description.Definition.Sleep == nil {
					t.Fatalf("checkpoint handoff = %+v, %v", description, err)
				}
			}
			_, err := manager.NextSourceSnapshot(t.Context(), "mig-1", "vm-1", 0)
			if (state == vm.SleepStateComa) != errors.Is(err, vm.ErrConflict) {
				t.Fatalf("pre-copy snapshot of a %s source: %v", state, err)
			}
			for range 2 {
				if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, true); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := manager.OpenSourceMemorySnapshotFile(t.Context(), "mig-1", "vm-1", 1, "memory"); !errors.Is(err, vm.ErrConflict) {
				t.Fatalf("offloaded source exposed memory for transfer: %v", err)
			}
			if err := manager.UnlockSource(t.Context(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
				t.Fatal("detached source unlocked without rollback")
			}
			for range 2 {
				if err := manager.StartSourceRollback(t.Context(), "mig-1", "vm-1"); err != nil {
					t.Fatal(err)
				}
			}
			if err := manager.UnlockSource(t.Context(), "mig-1", "vm-1"); err != nil {
				t.Fatal(err)
			}
			if host.normalizeCalls+host.saveMemoryCalls+host.restoreCalls != 0 || host.observed["vm-1"].SleepState != state {
				t.Fatalf("migration woke its source: %+v", host)
			}
			wantDisks := 2 // the pre-copy and the final snapshot
			if state == vm.SleepStateComa {
				wantDisks = 0
			}
			if got := len(manager.disks.(*fakeMigrationStorage).created); got != wantDisks {
				t.Fatalf("disk snapshots = %d, want %d", got, wantDisks)
			}
			if host.removeNetworkCalls != 1 || host.ensureNetworkCalls != 1 || manager.IsSourceLocked("vm-1") {
				t.Fatal("rollback did not reconnect and release source")
			}
		})
	}
}

func TestOffloadedDestinationKeepsCheckpointAcrossReadyAndFinish(t *testing.T) {
	for _, state := range []vm.SleepState{vm.SleepStateDeepSleeping, vm.SleepStateComa} {
		t.Run(string(state), func(t *testing.T) {
			manager, host, source := newMigrationManager(t)
			source.prepareDefinition = virtualMachineDefinition("vm-1")
			source.prepareDefinition.Specification.SleepAfterIdleSeconds = 60
			source.prepareDefinition.Sleep = sleepMigration(t, state)
			source.prepareState = vm.StateStopped
			source.memoryReceive = func(_ context.Context, _ string) error {
				t.Error("migration downloaded offloaded memory")
				return nil
			}
			source.nextSnapshot = SourceSnapshot{Sequence: 1, SizeBytes: 1024, GUID: "disk-1"}
			source.stopSnapshot = SourceSnapshot{Sequence: 2, SizeBytes: 0, GUID: "disk-1"}
			wantNext := 1
			if state == vm.SleepStateComa {
				source.stopSnapshot = SourceSnapshot{}
				wantNext = 0
			}
			disks := manager.disks.(*fakeMigrationStorage)
			disks.guid = "disk-1"
			if _, err := manager.CreateDestination(t.Context(), "mig-1", "vm-1", "https://10.0.0.3:9000", nil, true, false); err != nil {
				t.Fatal(err)
			}
			if err := manager.AdvanceDestination(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			awaitTransfer(t, manager, "vm-1")
			record, err := manager.store.readDestination("vm-1")
			if err != nil || record.State != destinationReady || !manager.IsDestinationReserved("vm-1") {
				t.Fatalf("destination = %+v, %v", record, err)
			}
			if record.usesMemorySnapshot() || record.memorySnapshotStatus() != "" || source.nextCalls != wantNext {
				t.Fatal("offloaded handoff entered live memory migration")
			}
			if host.desired["vm-1"].State != vm.StateRunning || host.observed["vm-1"].SleepState != state || host.sleep == nil {
				t.Fatal("destination did not preserve sleeping checkpoint")
			}
			manager.rootCancel()
			manager, err = newManager(host, source, disks, manager.kernels, ampleCapacity, 0, nil, manager.telemetry)
			if err != nil {
				t.Fatalf("restart lost the ready checkpoint: %v", err)
			}
			defer manager.rootCancel()
			if err := manager.RequestFinish(t.Context(), "mig-1"); err != nil {
				t.Fatal(err)
			}
			record, _ = manager.store.readDestination("vm-1")
			for range 2 {
				if err := manager.advanceFinish(t.Context(), record); err != nil {
					t.Fatal(err)
				}
			}
			if manager.IsDestinationReserved("vm-1") || host.applyStateCalls+host.restoreMemoryCalls+host.activateMemoryCalls != 0 {
				t.Fatal("finish activated the guest or retained its reservation")
			}
			wantDisks := 2 // the pre-copy and the final snapshot
			if state == vm.SleepStateComa {
				wantDisks = 0
			}
			if disks.received != wantDisks {
				t.Fatalf("disk receives = %d, want %d", disks.received, wantDisks)
			}
		})
	}
}

func TestOffloadedMigrationRejectsResizeAndDiskOnlyMode(t *testing.T) {
	definition := virtualMachineDefinition("vm-1")
	definition.Sleep = sleepMigration(t, vm.SleepStateDeepSleeping)
	if _, err := (&Resize{CPUMillicores: 2000, MemoryMiB: 2048, DiskMiB: 4096}).apply(definition); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("offloaded checkpoint accepted a resize: %v", err)
	}
	if err := definition.validateSleepMigration(vm.StateStopped, false); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("offloaded checkpoint accepted disk-only mode: %v", err)
	}
}

func TestComaSourceFinishesWithoutAFinalDiskSnapshot(t *testing.T) {
	manager, host, _ := newMigrationManager(t)
	host.create("vm-1", testSpecification())
	host.observed["vm-1"] = vm.ObservedRecord{Generation: 1, State: vm.StateStopped, SleepState: vm.SleepStateComa}
	host.sleep = sleepMigration(t, vm.SleepStateComa)
	if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1", true); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, true); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := manager.DestroySource(t.Context(), "mig-1", "vm-1"); err != nil {
			t.Fatal(err)
		}
	}
	if host.releaseStorageCalls != 1 || host.removeRuntimeCalls != 1 || host.restoreCalls != 0 {
		t.Fatalf("source cleanup was not idempotent: %+v", host)
	}
	if len(manager.disks.(*fakeMigrationStorage).created) != 0 {
		t.Fatal("coma cleanup created a disk snapshot")
	}
}

func TestTrafficToASleepingSourceFailsTheMigrationBeforeStop(t *testing.T) {
	manager, host, _ := newMigrationManager(t)
	host.create("vm-1", testSpecification())
	host.observed["vm-1"] = vm.ObservedRecord{Generation: 1, State: vm.StateStopped, SleepState: vm.SleepStateDeepSleeping}
	host.sleep = sleepMigration(t, vm.SleepStateDeepSleeping)
	if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1", true); err != nil {
		t.Fatal(err)
	}
	desired := host.desired["vm-1"]
	desired.Generation++
	host.desired["vm-1"] = desired
	if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, true); !errors.Is(err, ErrSourceWoke) {
		t.Fatalf("a woken source stopped: %v", err)
	}
	if err := manager.UnlockSource(t.Context(), "mig-1", "vm-1"); err != nil || manager.IsSourceLocked("vm-1") {
		t.Fatalf("a woken source stayed locked: %v", err)
	}
}
