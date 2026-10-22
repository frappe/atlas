package migration

import (
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
	for _, state := range []vm.SleepState{vm.SleepStateDeepSleeping, vm.SleepStateComatose} {
		t.Run(string(state), func(t *testing.T) {
			manager, host, _ := newMigrationManager(t)
			host.create("vm-1", testSpecification())
			host.observed["vm-1"] = vm.ObservedRecord{State: vm.StateStopped, SleepState: state}
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
			if _, err := manager.NextSourceSnapshot(t.Context(), "mig-1", "vm-1", 0); !errors.Is(err, vm.ErrConflict) {
				t.Fatalf("offloaded source accepted a live snapshot: %v", err)
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
			wantDisks := 1
			if state == vm.SleepStateComatose {
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
