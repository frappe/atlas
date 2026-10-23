package migration

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func virtualMachineDefinition(virtualMachineID string) VirtualMachineDefinition {
	return VirtualMachineDefinition{
		VirtualMachineID:        virtualMachineID,
		CreateFingerprint:       strings.Repeat("a", 64),
		Generation:              1,
		SpecificationGeneration: 1,
		DesiredState:            vm.StateRunning,
		Specification:           testSpecification(),
	}
}

func TestCreateDestinationPreservesTheMemorySnapshotFlag(t *testing.T) {
	for _, flag := range []bool{false, true} {
		manager, machines, _ := newMigrationManager(t)
		ctx := t.Context()
		const source = "https://10.0.0.3:9000"
		if _, err := manager.CreateDestination(ctx, "mig-1", "vm-1", source, nil, flag, false); err != nil {
			t.Fatal(err)
		}

		store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
		record, err := store.readDestination("vm-1")
		if err != nil {
			t.Fatal(err)
		}
		if record.HasMemorySnapshot != flag {
			t.Fatalf("stored memory snapshot flag = %v, want %v", record.HasMemorySnapshot, flag)
		}
		if _, err := manager.CreateDestination(ctx, "mig-1", "vm-1", source, nil, flag, false); err != nil {
			t.Fatalf("repeat request: %v", err)
		}
		if _, err := manager.CreateDestination(ctx, "mig-1", "vm-1", source, nil, !flag, false); !errors.Is(err, vm.ErrConflict) {
			t.Fatalf("changed memory snapshot flag: %v, want ErrConflict", err)
		}
	}
}

func TestAdvanceDestinationPreparesTheSource(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	source.prepareDefinition = virtualMachineDefinition("vm-1")
	source.prepareState = vm.StateRunning
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}

	record, err := migrationManager.store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.Definition == nil {
		t.Fatalf("record = %+v", record)
	}

	desired, err := machines.ReadDesired("vm-1")
	if err != nil {
		t.Fatalf("placeholder record missing: %v", err)
	}
	if desired.Specification.MemoryMiB != 2048 || desired.UserID == 0 {
		t.Fatalf("placeholder = %+v", desired)
	}
}

func TestAdvanceDestinationAppliesTheResize(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	source.prepareDefinition = virtualMachineDefinition("vm-1")
	source.prepareState = vm.StateRunning
	ctx := context.Background()
	resize := &Resize{CPUMillicores: 4000, MemoryMiB: 8192, DiskMiB: 8192}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", resize, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}

	desired, err := machines.ReadDesired("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	specification := desired.Specification
	if specification.CPUMillicores != 4000 || specification.MemoryMiB != 8192 || specification.DiskMiB != 8192 {
		t.Fatalf("specification = %+v", specification)
	}
	if desired.SpecificationGeneration != 2 {
		t.Fatalf("specification generation = %d, want 2", desired.SpecificationGeneration)
	}
	reservations, err := migrationManager.DestinationReservations(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(reservations) != 1 || reservations[0].MemoryMiB != 8192 || reservations[0].DiskMiB != 8192 {
		t.Fatalf("reservations = %+v", reservations)
	}
}

func TestAdvanceDestinationRejectsAResizeThatShrinksTheDisk(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	source.prepareDefinition = virtualMachineDefinition("vm-1")
	ctx := context.Background()
	resize := &Resize{CPUMillicores: 4000, MemoryMiB: 8192, DiskMiB: 1024}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", resize, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err == nil {
		t.Fatal("want a disk shrink error")
	}
	reservations, err := migrationManager.DestinationReservations(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(reservations) != 0 {
		t.Fatalf("a rejected resize reserved capacity: %+v", reservations)
	}
}

func TestAdvanceDestinationChecksCapacityForTheResize(t *testing.T) {
	machines := newFakeMigrationHost(t)
	source := &fakeSourceOperations{prepareDefinition: virtualMachineDefinition("vm-1"), prepareState: vm.StateStopped}
	sourceShapeOnly := func(context.Context) (AvailableCapacity, error) {
		return AvailableCapacity{MemoryMiB: 2048, StorageMiB: 4096}, nil
	}
	migrationManager, err := newManager(machines, source, &fakeMigrationStorage{}, &fakeKernelStore{}, sourceShapeOnly, 0, nil, telemetry.NewRegistry(nil))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	resize := &Resize{CPUMillicores: 2000, MemoryMiB: 4096, DiskMiB: 4096}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", resize, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("advance without resize capacity = %v, want ErrConflict", err)
	}
}

func TestCreateDestinationRejectsARetryWithAnotherResize(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	ctx := context.Background()
	resize := &Resize{CPUMillicores: 2000, MemoryMiB: 4096, DiskMiB: 4096}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", resize, false, false); err != nil {
		t.Fatal(err)
	}

	sameResize := *resize
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", &sameResize, false, false); err != nil {
		t.Fatalf("retry with the same resize = %v", err)
	}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("retry without the resize = %v, want ErrConflict", err)
	}
}

func TestAdvanceDestinationIsANoOpWhileCopying(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	source.prepareDefinition = virtualMachineDefinition("vm-1")
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if source.prepareCalls != 1 {
		t.Fatalf("PrepareSource calls = %d, want 1", source.prepareCalls)
	}
}

func TestAdvanceDestinationExpiresAStaleReservation(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	record.CreatedAt = time.Now().Add(-11 * time.Minute).UTC()
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	// Expiry requests an abort and the worker rolls the reservation back.
	awaitTransfer(t, migrationManager, "vm-1")
	if source.removeCalls != 1 {
		t.Fatalf("RemoveSource calls = %d, want 1", source.removeCalls)
	}
	if migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("reservation kept after expiry")
	}
}

func TestAdvanceDestinationRecordsSourcePreparationErrors(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	source.prepareError = errors.New("source unreachable")
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err == nil {
		t.Fatal("want a description error")
	}
	record, err := migrationManager.DestinationStatus(ctx, "mig-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.Error == nil || record.Phase != PhasePreparing {
		t.Fatalf("record = %+v", record)
	}
}

func TestAdvanceDestinationRejectsAWrongConfig(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	source.prepareDefinition = virtualMachineDefinition("vm-other")
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err == nil {
		t.Fatal("want a definition mismatch error")
	}
}

func TestAdvanceDestinationRejectsInsufficientCapacity(t *testing.T) {
	machines := newFakeMigrationHost(t)
	source := &fakeSourceOperations{prepareDefinition: virtualMachineDefinition("vm-1"), prepareState: vm.StateRunning}
	noCapacity := func(context.Context) (AvailableCapacity, error) { return AvailableCapacity{}, nil }
	migrationManager, err := newManager(machines, source, &fakeMigrationStorage{}, &fakeKernelStore{}, noCapacity, 0, nil, telemetry.NewRegistry(nil))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("advance without capacity = %v, want ErrConflict", err)
	}
}

func TestDestinationCapacityCheckAndReservationAreSerialized(t *testing.T) {
	machines := newFakeMigrationHost(t)
	var migrationManager *Manager
	capacity := func(ctx context.Context) (AvailableCapacity, error) {
		reservations, err := migrationManager.DestinationReservations(ctx)
		if err != nil {
			return AvailableCapacity{}, err
		}
		availableMemoryMiB := 3072
		for _, reservation := range reservations {
			availableMemoryMiB -= reservation.MemoryMiB
		}
		return AvailableCapacity{MemoryMiB: availableMemoryMiB, StorageMiB: 1 << 20}, nil
	}
	var err error
	migrationManager, err = newManager(machines, &fakeSourceOperations{}, &fakeMigrationStorage{}, &fakeKernelStore{}, capacity, 0, nil, telemetry.NewRegistry(nil))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	records := make([]destinationRecord, 2)
	for index, virtualMachineID := range []string{"vm-1", "vm-2"} {
		_, err := migrationManager.CreateDestination(ctx, "mig-"+virtualMachineID, virtualMachineID, "https://10.0.0.3:9000", nil, false, false)
		if err != nil {
			t.Fatal(err)
		}
		record, err := migrationManager.store.readDestination(virtualMachineID)
		if err != nil {
			t.Fatal(err)
		}
		records[index] = record
	}

	results := make(chan error, len(records))
	var waitGroup sync.WaitGroup
	for _, record := range records {
		waitGroup.Add(1)
		go func(record destinationRecord) {
			defer waitGroup.Done()
			definition := virtualMachineDefinition(record.VirtualMachineID)
			results <- migrationManager.reserveAndReconstructDestination(ctx, record, definition, vm.StateRunning)
		}(record)
	}
	waitGroup.Wait()
	close(results)

	succeeded, rejected := 0, 0
	for result := range results {
		switch {
		case result == nil:
			succeeded++
		case errors.Is(result, vm.ErrConflict):
			rejected++
		default:
			t.Fatalf("reservation result = %v", result)
		}
	}
	if succeeded != 1 || rejected != 1 {
		t.Fatalf("succeeded = %d, rejected = %d", succeeded, rejected)
	}
}

func TestActiveDestinationVirtualMachineIDsListsRunningDestinations(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}

	ids, err := migrationManager.ActiveDestinationVirtualMachineIDs(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 1 || ids[0] != "vm-1" {
		t.Fatalf("active destinations = %v", ids)
	}
}

// writeReadyDestination writes a ready destination record for finish tests.
func writeReadyDestination(t *testing.T, machines *fakeMigrationHost, mutate func(*destinationRecord)) *migrationStore {
	t.Helper()
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record := destinationRecord{
		ID:               "mig-1",
		VirtualMachineID: "vm-1",
		Source:           "https://10.0.0.3:9000",
		State:            destinationReady,
		CreatedAt:        time.Now().UTC(),
	}
	if mutate != nil {
		mutate(&record)
	}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	return store
}

func TestRunTransferFinishesToCompleted(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	store := writeReadyDestination(t, machines, func(r *destinationRecord) {
		r.State = destinationFinishing
		r.Intervals = []TransferProgress{
			{Sequence: 1, Completed: true},
			{Sequence: 2, Completed: true},
		}
	})

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State != destinationCompleted || record.FinishedAt.IsZero() {
		t.Fatalf("record = %+v", record)
	}
	if source.finishCalls != 1 {
		t.Fatalf("source finish calls = %d, want 1", source.finishCalls)
	}
	// Finish removes the received migration snapshots, so they do not pile up.
	if want := []string{"migration-mig-1-1", "migration-mig-1-2"}; !equalStringSlices(transfer.removed, want) {
		t.Fatalf("removed snapshots = %v", transfer.removed)
	}
}

func TestRequestFinishRecordsIntent(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeReadyDestination(t, machines, nil)

	if err := migrationManager.RequestFinish(context.Background(), "mig-1"); err != nil {
		t.Fatal(err)
	}
	record, _ := store.readDestination("vm-1")
	if record.State != destinationFinishing {
		t.Fatal("finish request was not recorded")
	}
}

func TestRequestFinishRequiresReady(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeCopyingDestination(t, machines, vm.StateRunning)

	if err := migrationManager.RequestFinish(context.Background(), "mig-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("finish before ready = %v, want ErrConflict", err)
	}
}

func TestRequestFinishConflictsWithAbort(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeReadyDestination(t, machines, func(r *destinationRecord) { r.State = destinationRemovingRuntime })

	if err := migrationManager.RequestFinish(context.Background(), "mig-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("finish after abort = %v, want ErrConflict", err)
	}
}

func TestAbortConflictsAfterTheDestinationBooted(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeReadyDestination(t, machines, nil)

	if err := migrationManager.AbortDestination(t.Context(), "mig-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("abort of a booted destination = %v, want ErrConflict", err)
	}
	if record, _ := store.readDestination("vm-1"); record.State != destinationReady {
		t.Fatalf("state = %s, want ready", record.State)
	}

	// A paused memory restore and an asleep offloaded VM have not run the guest, so they can still roll back.
	for _, mutate := range []func(*destinationRecord){
		func(r *destinationRecord) { r.HasMemorySnapshot = true },
		func(r *destinationRecord) {
			definition := virtualMachineDefinition("vm-1")
			definition.Sleep = sleepMigration(t, vm.SleepStateDeepSleeping)
			r.Definition = &definition
		},
	} {
		writeReadyDestination(t, machines, mutate)
		if err := migrationManager.AbortDestination(t.Context(), "mig-1"); err != nil {
			t.Fatalf("abort of a destination whose guest did not run = %v", err)
		}
	}
}

func TestRunTransferAbortsBeforeStop(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, _ := store.readDestination("vm-1")
	record.State = destinationRemovingRuntime
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	migrationManager.runTransfer(context.Background(), "vm-1")

	rec, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if rec.State.status() != StatusAborted || rec.State.phase() != "" {
		t.Fatalf("record = %+v", rec)
	}
	// A source that never stopped is unlocked, not restarted.
	if source.startCalls != 0 || source.removeCalls != 1 {
		t.Fatalf("start calls = %d, remove calls = %d", source.startCalls, source.removeCalls)
	}
	if transfer.aborts != 1 {
		t.Fatalf("receive aborts = %d, want 1", transfer.aborts)
	}
}

func TestRunTransferAbortsAfterStop(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, _ := store.readDestination("vm-1")
	record.SourceStopped = true
	record.State = destinationRemovingRuntime
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	migrationManager.runTransfer(context.Background(), "vm-1")

	rec, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if rec.State.status() != StatusAborted {
		t.Fatalf("status = %s, want aborted", rec.State.status())
	}
	// A stopped source is restored, then unlocked.
	if source.startCalls != 1 || source.removeCalls != 1 {
		t.Fatalf("start calls = %d, remove calls = %d", source.startCalls, source.removeCalls)
	}
}

func TestRunTransferKeepsLockedWhenRollbackFails(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	source.removeError = errors.New("source unreachable")
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, _ := store.readDestination("vm-1")
	record.State = destinationRemovingRuntime
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	migrationManager.runTransfer(context.Background(), "vm-1")

	rec, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if rec.State.terminal() {
		t.Fatal("a failed rollback must keep the migration locked")
	}
	if rec.State != destinationUnlockingSource {
		t.Fatal("the abort request must be preserved for a retry")
	}
}

func TestAbortRestoresSourceWhenTheStopReplyWasLost(t *testing.T) {
	for _, memory := range []bool{false, true} {
		sourceManager, sourceMachines, _ := newMigrationManager(t)
		seedSourceVM(t, sourceManager, sourceMachines, vm.StateRunning, 1)
		if _, err := sourceManager.StopSource(t.Context(), "mig-1", "vm-1", 1, memory); err != nil {
			t.Fatal(err)
		}
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var err error
			if r.Method == http.MethodDelete {
				err = sourceManager.UnlockSource(r.Context(), "mig-1", "vm-1")
			} else if r.URL.Path == "/v1/migrations/mig-1/start" {
				err = sourceManager.StartSourceRollback(r.Context(), "mig-1", "vm-1")
			} else {
				t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if errors.Is(err, vm.ErrConflict) {
				w.WriteHeader(http.StatusConflict)
			} else if err != nil {
				t.Errorf("source operation: %v", err)
				w.WriteHeader(http.StatusInternalServerError)
			} else {
				w.WriteHeader(http.StatusNoContent)
			}
		}))
		defer server.Close()
		manager, machines, _ := newMigrationManager(t)
		manager.source = NewSourceClient(0, nil)
		store := writeCopyingDestination(t, machines, vm.StateRunning)
		record, _ := store.readDestination("vm-1")
		record.State = destinationStopping
		record.Source = server.URL
		record.HasMemorySnapshot = memory
		if err := store.writeDestination(record); err != nil {
			t.Fatal(err)
		}
		if err := manager.requestAbort(t.Context(), "vm-1", false); err != nil {
			t.Fatal(err)
		}
		manager.runTransfer(t.Context(), "vm-1")
		record, _ = store.readDestination("vm-1")
		if record.State != destinationAborted || sourceManager.IsSourceLocked("vm-1") {
			t.Fatalf("memory=%t: rollback left destination %s or source locked", memory, record.State)
		}
		if sourceMachines.restoreCalls != 1 || sourceMachines.restoreMemory != memory {
			t.Fatalf("memory=%t: source restored %d times with memory=%t", memory, sourceMachines.restoreCalls, sourceMachines.restoreMemory)
		}
	}
}

func TestDiscardAfterAnOperatorRestoredTheSource(t *testing.T) {
	sourceManager, sourceMachines, _ := newMigrationManager(t)
	seedSourceVM(t, sourceManager, sourceMachines, vm.StateRunning, 1)
	if _, err := sourceManager.StopSource(t.Context(), "mig-1", "vm-1", 1, false); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := sourceManager.RestoreSource(t.Context(), "mig-1", "vm-1"); err != nil {
			t.Fatal(err)
		}
	}
	if sourceMachines.restoreCalls != 1 || sourceManager.IsSourceLocked("vm-1") {
		t.Fatalf("restore calls = %d, locked = %t", sourceMachines.restoreCalls, sourceManager.IsSourceLocked("vm-1"))
	}

	// The lost destination comes back at ready with a booted guest.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var err error
		if r.Method == http.MethodDelete {
			err = sourceManager.UnlockSource(r.Context(), "mig-1", "vm-1")
		} else {
			err = sourceManager.StartSourceRollback(r.Context(), "mig-1", "vm-1")
		}
		switch {
		case errors.Is(err, vm.ErrNotFound):
			w.WriteHeader(http.StatusNotFound)
		case err != nil:
			w.WriteHeader(http.StatusInternalServerError)
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	manager, machines, _ := newMigrationManager(t)
	manager.source = NewSourceClient(0, nil)
	store := writeReadyDestination(t, machines, func(r *destinationRecord) {
		r.Source = server.URL
		r.SourceStopped = true
	})

	if err := manager.AbortDestination(t.Context(), "mig-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("abort = %v, want ErrConflict", err)
	}
	if err := manager.DiscardDestination(t.Context(), "mig-1"); err != nil {
		t.Fatal(err)
	}
	manager.runTransfer(t.Context(), "vm-1")
	if record, _ := store.readDestination("vm-1"); record.State != destinationAborted {
		t.Fatalf("state = %s, want aborted", record.State)
	}
	if sourceMachines.restoreCalls != 1 {
		t.Fatalf("source restored %d times, want 1", sourceMachines.restoreCalls)
	}
}

func TestAdvanceDestinationRollsBackAnAbandonedDestination(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	// Atlas stopped calling, so the destination must release its reservation.
	record.State = destinationCopying
	record.LastControlAt = time.Now().Add(-destinationIdleTimeout - time.Minute).UTC()
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	awaitTransfer(t, migrationManager, "vm-1")

	if source.removeCalls != 1 {
		t.Fatalf("RemoveSource calls = %d, want 1", source.removeCalls)
	}
	if migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("an abandoned destination must release its reservation")
	}
}

func TestAdvanceDestinationKeepsAReadyDestination(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil, false, false); err != nil {
		t.Fatal(err)
	}
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	// A ready destination may already own the VM.
	record.State = destinationReady
	record.LastControlAt = time.Now().Add(-destinationIdleTimeout - time.Hour).UTC()
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}

	if !migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("a ready destination must keep its reservation")
	}
}

// writeTerminalDestination writes a terminal destination record.
func writeTerminalDestination(t *testing.T, machines *fakeMigrationHost, state destinationState) *migrationStore {
	t.Helper()
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record := destinationRecord{
		ID: "mig-1", VirtualMachineID: "vm-1", State: state,
		CreatedAt: time.Now().UTC(), FinishedAt: time.Now().UTC(),
	}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	return store
}

func TestTerminalDestinationDoesNotHideOrReserve(t *testing.T) {
	for _, state := range []destinationState{destinationCompleted, destinationAborted} {
		migrationManager, machines, _ := newMigrationManager(t)
		writeTerminalDestination(t, machines, state)
		if migrationManager.IsDestinationReserved("vm-1") {
			t.Fatalf("%s record still reserves the VM", state)
		}
		reservations, err := migrationManager.DestinationReservations(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if len(reservations) != 0 {
			t.Fatalf("%s record still reserves capacity: %+v", state, reservations)
		}
	}
}

func TestCreateDestinationReplacesACleanAbortedRemnant(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeTerminalDestination(t, machines, destinationAborted)

	record, err := migrationManager.CreateDestination(context.Background(), "mig-2", "vm-1", "https://10.0.0.9:9000", nil, false, false)
	if err != nil {
		t.Fatalf("replace a clean aborted remnant = %v", err)
	}
	if record.ID != "mig-2" || record.Status != StatusRunning || record.Phase != PhasePreparing {
		t.Fatalf("record = %+v", record)
	}
}

func TestRemoveAbortedDestinationsKeepsRecentAndCompletedRecords(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeTerminalDestination(t, machines, destinationAborted)
	if err := migrationManager.RemoveAbortedDestinations(t.Context()); err != nil {
		t.Fatal(err)
	}
	if !store.has(store.destinationPath("vm-1")) {
		t.Fatal("a recent aborted record was removed")
	}

	jail := filepath.Join(filepath.Dir(store.migrationDirectory("vm-1")), "firecracker")
	if err := os.Mkdir(jail, 0o750); err != nil {
		t.Fatal(err)
	}
	migrationManager.now = func() time.Time { return time.Now().Add(abortedRecordRetention + time.Hour) }
	if err := migrationManager.RemoveAbortedDestinations(t.Context()); err != nil {
		t.Fatal(err)
	}
	if store.has(filepath.Dir(store.migrationDirectory("vm-1"))) {
		t.Fatal("an old aborted remnant was kept")
	}

	writeTerminalDestination(t, machines, destinationCompleted)
	if err := migrationManager.RemoveAbortedDestinations(t.Context()); err != nil {
		t.Fatal(err)
	}
	if !store.has(store.destinationPath("vm-1")) {
		t.Fatal("a completed record was removed")
	}
}

func TestCreateDestinationRefusesReplacingACompletedRecord(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeTerminalDestination(t, machines, destinationCompleted)

	if _, err := migrationManager.CreateDestination(context.Background(), "mig-2", "vm-1", "https://10.0.0.9:9000", nil, false, false); err == nil {
		t.Fatal("a completed migration must not be replaced")
	}
}

func TestActiveDestinationsIncludeFinishAndAbortWork(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	// Ready finish and failed abort requests are both requeued.
	if err := store.writeDestination(destinationRecord{
		ID: "mig-1", VirtualMachineID: "vm-1", State: destinationFinishing, CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.writeDestination(destinationRecord{
		ID: "mig-2", VirtualMachineID: "vm-2", State: destinationRemovingRuntime, CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.writeDestination(terminalDestinationRecord(destinationRecord{ID: "mig-3", VirtualMachineID: "vm-3"}, destinationCompleted, time.Now().UTC())); err != nil {
		t.Fatal(err)
	}

	active, err := migrationManager.ActiveDestinationVirtualMachineIDs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(active) != 2 {
		t.Fatalf("active = %v, want the two nonterminal migrations", active)
	}
}
