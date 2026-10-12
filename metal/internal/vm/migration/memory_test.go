package migration

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestReceiveMemorySnapshotChecksFiles(t *testing.T) {
	for _, failure := range []string{"", "checksum", "truncated", "oversized", "missing"} {
		t.Run(failure, func(t *testing.T) {
			server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.URL.Query().Get("sequence") != "2" || request.URL.Query().Get("virtual_machine_id") != "vm-1" {
					t.Error("request lost snapshot identity")
				}
				if failure == "missing" {
					writer.WriteHeader(http.StatusNotFound)
					return
				}
				contents := []byte(request.URL.Query().Get("file"))
				if failure == "oversized" && request.URL.Query().Get("file") == "memory" {
					contents = make([]byte, (1<<20)+1)
				}
				digest := sha256.Sum256(contents)
				writer.Header().Set("Trailer", "X-Snapshot-SHA256")
				_, _ = writer.Write(contents)
				if failure == "checksum" {
					digest[0] ^= 1
				}
				if failure != "truncated" {
					writer.Header().Set("X-Snapshot-SHA256", hex.EncodeToString(digest[:]))
				}
			}))
			defer server.Close()
			client := NewSourceClient(0, nil)
			client.transferClient = server.Client()
			directory := t.TempDir()
			err := client.ReceiveMemorySnapshot(t.Context(), server.URL, "mig-1", "vm-1", 2, directory, 1)
			if (err != nil) != (failure != "") {
				t.Fatalf("error = %v", err)
			}
			if failure == "" {
				for _, name := range []string{"state", "memory", "metadata.json"} {
					contents, err := os.ReadFile(filepath.Join(directory, name))
					if err != nil || string(contents) != name {
						t.Fatalf("%s = %q, %v", name, contents, err)
					}
				}
			}
		})
	}
}

func TestFinalTransfersOverlapAndCheckpointIndependently(t *testing.T) {
	manager, machines, source := newMigrationManager(t)
	seedDestinationVM(t, machines, vm.StateRunning)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, _ := store.readDestination("vm-1")
	record.State = destinationStopping
	record.MigrateMemorySnapshot = true
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	source.stopSnapshot = SourceSnapshot{Sequence: 1, SizeBytes: 100, GUID: "disk-guid"}
	disks := manager.disks.(*fakeMigrationStorage)
	disks.guid = "disk-guid"
	memoryStarted, diskStarted := make(chan struct{}), make(chan struct{})
	releaseMemory, releaseDisk := make(chan struct{}), make(chan struct{})
	source.memoryReceive = func(ctx context.Context, directory string) error {
		close(memoryStarted)
		select {
		case <-releaseMemory:
		case <-ctx.Done():
			return ctx.Err()
		}
		return os.WriteFile(filepath.Join(directory, "memory"), []byte("guest memory"), 0o600)
	}
	disks.receive = func(ctx context.Context) error {
		close(diskStarted)
		select {
		case <-releaseDisk:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	finished := make(chan struct{})
	go func() { manager.runTransfer(ctx, "vm-1"); close(finished) }()
	for _, started := range []chan struct{}{memoryStarted, diskStarted} {
		select {
		case <-started:
		case <-ctx.Done():
			t.Fatal("transfers did not overlap")
		}
	}
	close(releaseDisk)
	select {
	case <-finished:
		t.Fatal("migration advanced before memory completed")
	default:
	}
	close(releaseMemory)
	select {
	case <-finished:
	case <-ctx.Done():
		t.Fatal("transfers did not finish")
	}
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State != destinationReady || record.MemorySnapshotGUID != "disk-guid" || !source.stopMemorySnapshot {
		t.Fatalf("record = %+v, memory requested = %v", record, source.stopMemorySnapshot)
	}
	if _, err := os.Stat(filepath.Join(store.memorySnapshotDirectory("vm-1"), "disk-snapshot.json")); err != nil {
		t.Fatal(err)
	}
	// Re-enter cutover after both checkpoints; neither transfer should run again.
	record.State = destinationStopping
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	manager.runTransfer(ctx, "vm-1")
	if disks.received != 1 {
		t.Fatalf("disk received %d times", disks.received)
	}
}

func TestFinalTransferFailureCancelsItsPeer(t *testing.T) {
	for _, failing := range []string{"disk", "memory"} {
		t.Run(failing, func(t *testing.T) {
			manager, machines, source := newMigrationManager(t)
			seedDestinationVM(t, machines, vm.StateRunning)
			store := writeCopyingDestination(t, machines, vm.StateRunning)
			record, _ := store.readDestination("vm-1")
			record.State, record.MigrateMemorySnapshot = destinationStopping, true
			if err := store.writeDestination(record); err != nil {
				t.Fatal(err)
			}
			disks := manager.disks.(*fakeMigrationStorage)
			started := make(chan struct{})
			peer := func(ctx context.Context) error { close(started); <-ctx.Done(); return ctx.Err() }
			failure := errors.New("transfer interrupted")
			if failing == "disk" {
				source.memoryReceive = func(ctx context.Context, _ string) error { return peer(ctx) }
				disks.receive = func(context.Context) error { <-started; return failure }
			} else {
				disks.receive = peer
				source.memoryReceive = func(context.Context, string) error { <-started; return failure }
			}
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			err := manager.receiveFinalSnapshots(ctx, record, SourceSnapshot{Sequence: 1, GUID: "guid"})
			if !errors.Is(err, failure) || ctx.Err() != nil {
				t.Fatalf("error = %v, context = %v", err, ctx.Err())
			}
			record, _ = store.readDestination("vm-1")
			if record.MemorySnapshotGUID != "" || record.State != destinationStopping {
				t.Fatalf("record = %+v", record)
			}
			if _, err := os.Stat(store.memorySnapshotDirectory("vm-1")); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("published partial state: %v", err)
			}
		})
	}
}

func TestMemorySourceIsBoundToStoppedFinalSnapshot(t *testing.T) {
	manager, machines, _ := newMigrationManager(t)
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateRunning)
	if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.OpenSourceMemoryFile(t.Context(), "mig-1", "vm-1", 1, "memory"); !errors.Is(err, vm.ErrConflict) {
		t.Fatal(err)
	}
	if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, true); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, true); err != nil {
		t.Fatal(err)
	}
	if machines.saveMemoryCalls != 1 || machines.normalizeCalls != 0 {
		t.Fatalf("saves %d, normal stops %d", machines.saveMemoryCalls, machines.normalizeCalls)
	}
	if _, err := manager.StopSource(t.Context(), "mig-1", "vm-1", 0, false); !errors.Is(err, vm.ErrConflict) {
		t.Fatal(err)
	}
	directory := filepath.Join(machines.dir, "vm-1", "saved-state")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "memory"), []byte("memory"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"../config.json", "memory"} {
		sequence := 1
		if name == "memory" {
			sequence = 2
		}
		if _, err := manager.OpenSourceMemoryFile(t.Context(), "mig-1", "vm-1", sequence, name); !errors.Is(err, vm.ErrConflict) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	file, err := manager.OpenSourceMemoryFile(t.Context(), "mig-1", "vm-1", 1, "memory")
	if err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := manager.CreateDestination(t.Context(), "mig-2", "vm-2", "https://host:9000", &Resize{MemoryMiB: 1}, true); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("memory resize accepted: %v", err)
	}
	if err := manager.StartSourceRollback(t.Context(), "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if !machines.restoreMemory {
		t.Fatal("source rollback did not request saved memory")
	}
}

func TestAbortRemovesTransferredAndPartialMemory(t *testing.T) {
	manager, machines, _ := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	record.State, record.SourceStopped = destinationRemovingStorage, true
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	directory := store.memorySnapshotDirectory("vm-1")
	for _, path := range []string{directory, directory + "-pending"} {
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := manager.cleanAbortDestination(t.Context(), record); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{directory, directory + "-pending"} {
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("memory left after abort: %v", err)
		}
	}
}

func TestMemoryRestoreChecksDiskBeforeReadyAndActivatesBeforeCleanup(t *testing.T) {
	for _, failing := range []string{"", "disk_guid", "checkpoint", "restore", "activation"} {
		t.Run(failing, func(t *testing.T) {
			manager, machines, source := newMigrationManager(t)
			store := writeCopyingDestination(t, machines, vm.StateRunning)
			record, err := store.readDestination("vm-1")
			if err != nil {
				t.Fatal(err)
			}
			record.State, record.MigrateMemorySnapshot, record.SourceStopped = destinationApplyingState, true, true
			record.MemorySnapshotGUID = "final-guid"
			record.Intervals = []TransferProgress{{Sequence: 2, Completed: true, GUID: "final-guid"}}
			if failing == "checkpoint" {
				record.MemorySnapshotGUID = "other-guid"
			}
			if err := store.writeDestination(record); err != nil {
				t.Fatal(err)
			}
			directory := store.memorySnapshotDirectory("vm-1")
			if err := os.Mkdir(directory, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := writeMigrationRecord(filepath.Join(directory, "disk-snapshot.json"), SourceSnapshot{Sequence: 2, GUID: "final-guid"}); err != nil {
				t.Fatal(err)
			}
			disks := manager.disks.(*fakeMigrationStorage)
			disks.guid = "final-guid"
			if failing == "disk_guid" {
				disks.guid = "other-guid"
			}
			if failing == "restore" {
				machines.restoreMemoryError = errors.New("load failed")
			}
			manager.runTransfer(t.Context(), "vm-1")
			record, err = store.readDestination("vm-1")
			if err != nil {
				t.Fatal(err)
			}
			if failing != "" && failing != "activation" {
				if record.State != destinationFailed || source.finishCalls != 0 || machines.activateMemoryCalls != 0 {
					t.Fatalf("record = %+v", record)
				}
				if failing != "restore" && machines.restoreMemoryCalls != 0 {
					t.Fatal("loaded memory before disk validation")
				}
				return
			}
			if record.State != destinationReady || machines.restoreMemoryCalls != 1 || machines.applyStateCalls != 0 || machines.activateMemoryCalls != 0 {
				t.Fatalf("ready without a paused memory restore: %+v", record)
			}
			machines.activateMemory = func() error {
				if source.finishCalls != 0 {
					t.Error("source destroyed before activation")
				}
				if failing == "activation" {
					return errors.New("resume failed")
				}
				return nil
			}
			if err := manager.RequestFinish(t.Context(), "mig-1"); err != nil {
				t.Fatal(err)
			}
			manager.runTransfer(t.Context(), "vm-1")
			record, err = store.readDestination("vm-1")
			if err != nil {
				t.Fatal(err)
			}
			if failing == "activation" {
				if record.State != destinationFinishing || source.finishCalls != 0 {
					t.Fatal("activation failure lost forward recovery")
				}
				if err := manager.AbortDestination(t.Context(), "mig-1"); !errors.Is(err, vm.ErrConflict) {
					t.Fatal("rollback allowed after ownership commit")
				}
				return
			}
			if record.State != destinationCompleted || source.finishCalls != 1 {
				t.Fatalf("record = %+v", record)
			}
			if _, err := os.Stat(directory); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("staging remained after finish")
			}
		})
	}
}
