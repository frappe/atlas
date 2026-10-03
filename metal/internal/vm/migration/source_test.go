package migration

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

// writeSourceRecord sets up a source lock for transfer tests.
func writeSourceRecord(t *testing.T, machines *fakeMigrationHost) {
	t.Helper()
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record := sourceRecord{ID: "mig-1", VirtualMachineID: "vm-1", State: sourceLocked}
	if err := store.writeSource(record); err != nil {
		t.Fatal(err)
	}
}

func TestNextSourceSnapshotWalksSequences(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "g"
	transfer.sizeBytes = 1000
	writeSourceRecord(t, machines)
	ctx := context.Background()

	first, err := migrationManager.NextSourceSnapshot(ctx, "mig-1", "vm-1", 0)
	if err != nil || first.Sequence != 1 || first.SizeBytes != 1000 || first.GUID != "g" {
		t.Fatalf("first = %+v, %v", first, err)
	}

	second, err := migrationManager.NextSourceSnapshot(ctx, "mig-1", "vm-1", 1)
	if err != nil || second.Sequence != 2 {
		t.Fatalf("second = %+v, %v", second, err)
	}
	third, err := migrationManager.NextSourceSnapshot(ctx, "mig-1", "vm-1", 2)
	if err != nil || third.Sequence != 3 {
		t.Fatalf("third = %+v, %v", third, err)
	}
	if want := []string{"migration-mig-1-1", "migration-mig-1-2", "migration-mig-1-3"}; !equalStringSlices(transfer.created, want) {
		t.Fatalf("created = %v", transfer.created)
	}
	// Acknowledging 2 removes 1 and keeps 2 as the base.
	if want := []string{"migration-mig-1-1"}; !equalStringSlices(transfer.removed, want) {
		t.Fatalf("removed = %v", transfer.removed)
	}
}

func TestNextSourceSnapshotIsIdempotent(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	writeSourceRecord(t, machines)
	ctx := context.Background()

	if _, err := migrationManager.NextSourceSnapshot(ctx, "mig-1", "vm-1", 0); err != nil {
		t.Fatal(err)
	}
	created := len(transfer.created)
	repeat, err := migrationManager.NextSourceSnapshot(ctx, "mig-1", "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	if repeat.Sequence != 1 || len(transfer.created) != created {
		t.Fatalf("repeat = %+v, created %d", repeat, len(transfer.created))
	}
}

func TestNextSourceSnapshotRejectsAnotherMigration(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeSourceRecord(t, machines)

	if _, err := migrationManager.NextSourceSnapshot(context.Background(), "mig-2", "vm-1", 0); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("other migration = %v, want ErrConflict", err)
	}
}

func TestSourceRejectsInvalidAcknowledgedSequence(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	writeSourceRecord(t, machines)
	if _, err := migrationManager.NextSourceSnapshot(t.Context(), "mig-1", "vm-1", 0); err != nil {
		t.Fatal(err)
	}

	for _, receivedSequence := range []int{-1, 2} {
		if _, err := migrationManager.NextSourceSnapshot(t.Context(), "mig-1", "vm-1", receivedSequence); !errors.Is(err, vm.ErrConflict) {
			t.Errorf("next snapshot sequence %d = %v, want ErrConflict", receivedSequence, err)
		}
		if _, err := migrationManager.StopSource(t.Context(), "mig-1", "vm-1", receivedSequence); !errors.Is(err, vm.ErrConflict) {
			t.Errorf("stop source sequence %d = %v, want ErrConflict", receivedSequence, err)
		}
	}
}

func TestStartSourceStreamRejectsAnUnknownSequence(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	if err := store.writeSource(sourceRecord{ID: "mig-1", VirtualMachineID: "vm-1", State: sourceLocked, Sequence: 2}); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if err := migrationManager.StartSourceStream(ctx, "mig-1", "vm-1", 2, "", 0); err != nil {
		t.Fatal(err)
	}
	if want := []string{"migration-mig-1-2|migration-mig-1-1|"}; !equalStringSlices(transfer.sent, want) {
		t.Fatalf("sent = %v", transfer.sent)
	}
	if err := migrationManager.StartSourceStream(ctx, "mig-1", "vm-1", 3, "", 0); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("unknown sequence = %v, want ErrConflict", err)
	}
}

func TestUnlockWaitsUntilAStartingStreamIsRegistered(t *testing.T) {
	host := newFakeMigrationHost(t)
	host.operationCalls = make(chan string, 2)
	serverRelease := make(chan struct{})
	storage := &fakeMigrationStorage{sendStarted: make(chan struct{}), serverRelease: serverRelease}
	manager, err := newManager(host, &fakeSourceOperations{}, storage, ampleCapacity, 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.store.writeSource(sourceRecord{
		ID: "mig-1", VirtualMachineID: "vm-1", State: sourceLocked, Sequence: 1,
	}); err != nil {
		t.Fatal(err)
	}

	streamResult := make(chan error, 1)
	go func() {
		streamResult <- manager.StartSourceStream(t.Context(), "mig-1", "vm-1", 1, "", 0)
	}()
	<-host.operationCalls
	<-storage.sendStarted

	unlockResult := make(chan error, 1)
	go func() {
		unlockResult <- manager.UnlockSource(t.Context(), "mig-1", "vm-1")
	}()
	<-host.operationCalls
	select {
	case err := <-unlockResult:
		t.Fatalf("unlock passed the starting stream: %v", err)
	default:
	}

	close(serverRelease)
	if err := <-streamResult; err != nil {
		t.Fatal(err)
	}
	if err := <-unlockResult; err != nil {
		t.Fatal(err)
	}
	if manager.IsSourceLocked("vm-1") {
		t.Fatal("source remained locked")
	}
}

// seedSourceVM creates a source VM and lock with the given observed state.
func seedSourceVM(t *testing.T, migrationManager *Manager, machines *fakeMigrationHost, state vm.State, acknowledged int) {
	t.Helper()
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", state)
	record := sourceRecord{
		ID: "mig-1", VirtualMachineID: "vm-1",
		State: sourceLocked, Sequence: acknowledged, AcknowledgedSequence: acknowledged,
	}
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}
}

func TestStopSourceStopsAndCreatesFinalSnapshot(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "final-guid"
	transfer.sizeBytes = 500
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 2)

	snapshot, err := migrationManager.StopSource(context.Background(), "mig-1", "vm-1", 2)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Sequence != 3 || snapshot.SizeBytes != 500 || snapshot.GUID != "final-guid" {
		t.Fatalf("snapshot = %+v", snapshot)
	}
	if machines.normalizeCalls != 1 {
		t.Fatalf("normalize calls = %d, want 1", machines.normalizeCalls)
	}
	if machines.removeNetworkCalls != 1 {
		t.Fatalf("remove network calls = %d, want 1", machines.removeNetworkCalls)
	}
	// The source removes the stale candidate before the final snapshot.
	if want := []string{"migration-mig-1-3"}; !equalStringSlices(transfer.removed, want) {
		t.Fatalf("removed = %v", transfer.removed)
	}
	if want := []string{"migration-mig-1-3"}; !equalStringSlices(transfer.created, want) {
		t.Fatalf("created = %v", transfer.created)
	}
}

func TestStopSourceAcknowledgesTheLastReceivedSnapshot(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "final-guid"
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 2)

	record, err := migrationManager.store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	record.Sequence = 3
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	snapshot, err := migrationManager.StopSource(context.Background(), "mig-1", "vm-1", 3)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Sequence != 4 {
		t.Fatalf("final sequence = %d, want 4", snapshot.Sequence)
	}
	if want := []string{"migration-mig-1-4"}; !equalStringSlices(transfer.removed, want) {
		t.Fatalf("removed = %v, want %v", transfer.removed, want)
	}
}

func TestStopSourceRejectsASequenceTheSourceDidNotCreate(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 2)

	_, err := migrationManager.StopSource(context.Background(), "mig-1", "vm-1", 3)
	if !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("stop source = %v, want ErrConflict", err)
	}
}

func TestStopSourceIsIdempotent(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 0)
	ctx := context.Background()

	first, err := migrationManager.StopSource(ctx, "mig-1", "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	created := len(transfer.created)

	second, err := migrationManager.StopSource(ctx, "mig-1", "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	if second.Sequence != first.Sequence || first.Sequence != 1 {
		t.Fatalf("first = %+v, second = %+v", first, second)
	}
	if machines.normalizeCalls != 1 || machines.removeNetworkCalls != 1 || len(transfer.created) != created {
		t.Fatalf("repeat did work again: normalize %d, remove network %d, created %d", machines.normalizeCalls, machines.removeNetworkCalls, len(transfer.created))
	}
}

func TestStartSourceStreamAppliesDiskLimit(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 1)
	ctx := context.Background()

	if err := migrationManager.StartSourceStream(ctx, "mig-1", "vm-1", 1, "", 32); err != nil {
		t.Fatal(err)
	}
	if machines.limitDiskValue != 32 {
		t.Fatalf("applied disk limit = %d, want 32", machines.limitDiskValue)
	}
	record, err := migrationManager.store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.TemporaryDiskLimitMiBps != 32 {
		t.Fatalf("persisted disk limit = %d, want 32", record.TemporaryDiskLimitMiBps)
	}
}

func TestStartSourceStreamRejectsASecondHostStreamBeforeChangingItsDiskLimit(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.sendHang = true
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 1)
	if err := migrationManager.store.writeSource(sourceRecord{
		ID: "mig-2", VirtualMachineID: "vm-2", State: sourceLocked, Sequence: 1,
	}); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.StartSourceStream(context.Background(), "mig-1", "vm-1", 1, "", 32); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.StartSourceStream(context.Background(), "mig-2", "vm-2", 1, "", 64); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("second stream = %v, want ErrConflict", err)
	}
	if machines.limitDiskCalls != 1 {
		t.Fatalf("disk limit calls = %d, want 1", machines.limitDiskCalls)
	}
	if err := migrationManager.stopSourceStream(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
}

func TestShutdownStopsAnActiveSourceStream(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.sendHang = true
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 1)

	if err := migrationManager.StartSourceStream(context.Background(), "mig-1", "vm-1", 1, "", 0); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	migrationManager.sourceStreamsMutex.Lock()
	defer migrationManager.sourceStreamsMutex.Unlock()
	if len(migrationManager.sourceStreams) != 0 {
		t.Fatal("source stream remains after shutdown")
	}
}

func TestUnlockSourceRemovesSnapshotsAndRestoresDiskLimit(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 2)
	record, _ := migrationManager.store.readSource("vm-1")
	record.TemporaryDiskLimitMiBps = 32
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.UnlockSource(context.Background(), "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if want := []string{"migration-mig-1-1", "migration-mig-1-2"}; !equalStringSlices(transfer.removed, want) {
		t.Fatalf("removed = %v", transfer.removed)
	}
	if machines.refreshDiskCalls != 1 {
		t.Fatalf("disk refresh = %d, want the configured limit restored", machines.refreshDiskCalls)
	}
	if _, err := migrationManager.store.readSource("vm-1"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("source record still present: %v", err)
	}
}

func TestUnlockSourceRefusesAStoppedSourceBeforeRollback(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 1)
	record, _ := migrationManager.store.readSource("vm-1")
	record.State = sourceStopped
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.UnlockSource(context.Background(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("unlock a stopped source before rollback = %v, want ErrConflict", err)
	}
}

func TestStartSourceRollbackRestoresNetworkAndState(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateStopped, 1)
	record, _ := migrationManager.store.readSource("vm-1")
	record.State = sourceStopped
	record.OriginalDesired = vm.StateRunning
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.StartSourceRollback(context.Background(), "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if machines.ensureNetworkCalls != 1 || machines.restoreCalls != 1 {
		t.Fatalf("ensure network = %d, restore state = %d", machines.ensureNetworkCalls, machines.restoreCalls)
	}
	restored, _ := migrationManager.store.readSource("vm-1")
	if restored.State != sourceRestored {
		t.Fatal("rollback checkpoint was not written")
	}
}

func TestDestroySourceDestroysAndRemoves(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateStopped, 1)
	record, _ := migrationManager.store.readSource("vm-1")
	record.State = sourceDetached
	record.FinalSequence = 1
	if err := migrationManager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.DestroySource(context.Background(), "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if machines.removeRuntimeCalls != 1 || machines.releaseStorageCalls != 1 {
		t.Fatalf("remove runtime = %d, release storage = %d", machines.removeRuntimeCalls, machines.releaseStorageCalls)
	}
	if _, err := machines.ReadDesired("vm-1"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("VM records still present: %v", err)
	}
}

func TestDestroySourceRequiresAStoppedSource(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	seedSourceVM(t, migrationManager, machines, vm.StateRunning, 1)
	if err := migrationManager.DestroySource(context.Background(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("destroy a source that never stopped = %v, want ErrConflict", err)
	}
}

func TestDestroySourceIsIdempotentWhenGone(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	if err := migrationManager.DestroySource(context.Background(), "mig-1", "vm-1"); err != nil {
		t.Fatalf("destroy an already-gone source = %v, want nil", err)
	}
}

func TestDestroySourceRefusesARemnantWithoutASourceRecord(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateStopped)
	if err := migrationManager.DestroySource(context.Background(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("destroy a VM remnant with no source record = %v, want ErrConflict", err)
	}
}

func equalStringSlices(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func setObservedState(t *testing.T, machines *fakeMigrationHost, virtualMachineID string, state vm.State) {
	t.Helper()
	machines.observed[virtualMachineID] = vm.ObservedRecord{State: state, Generation: 1, UpdatedAt: time.Now().UTC()}
}

func TestLockSourceReturnsVirtualMachineDefinitionAndLocks(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateRunning)

	description, err := migrationManager.LockSource(ctx, "mig-1", "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if description.ObservedState != vm.StateRunning {
		t.Fatalf("observed = %s", description.ObservedState)
	}
	if description.Definition.VirtualMachineID != "vm-1" || description.Definition.CreateFingerprint == "" ||
		description.Definition.Specification.MemoryMiB != 2048 {
		t.Fatalf("definition = %+v", description.Definition)
	}
	if !migrationManager.IsSourceLocked("vm-1") {
		t.Fatal("VM was not source locked")
	}
}

func TestLockSourceRejectsAMissingVirtualMachine(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	if _, err := migrationManager.LockSource(context.Background(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("missing VM = %v, want ErrNotFound", err)
	}
}

func TestLockSourceRejectsFailedOrUnknownStates(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	machines.create("vm-1", testSpecification())

	// A fresh VM reports unknown state.
	if _, err := migrationManager.LockSource(ctx, "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("unknown state = %v, want ErrConflict", err)
	}

	setObservedState(t, machines, "vm-1", vm.StateFailed)
	if _, err := migrationManager.LockSource(ctx, "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("failed state = %v, want ErrConflict", err)
	}
}

func TestLockSourceIsIdempotentForOneMigration(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateRunning)
	if _, err := migrationManager.LockSource(ctx, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}

	if _, err := migrationManager.LockSource(ctx, "mig-1", "vm-1"); err != nil {
		t.Fatalf("repeat lock = %v, want nil", err)
	}
	if _, err := migrationManager.LockSource(ctx, "mig-2", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("second migration = %v, want ErrConflict", err)
	}
}

func TestUnlockSourceClearsTheLock(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateRunning)
	if _, err := migrationManager.LockSource(ctx, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.UnlockSource(ctx, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if migrationManager.IsSourceLocked("vm-1") {
		t.Fatal("source lock still present after unlock")
	}
	if err := migrationManager.UnlockSource(ctx, "mig-1", "vm-1"); err != nil {
		t.Fatalf("repeat unlock = %v, want nil", err)
	}
}

// expirableSourceRecord returns a pre-stop lock that has been idle past the timeout.
func expirableSourceRecord(manager *Manager, virtualMachineID string) sourceRecord {
	return sourceRecord{
		ID:               "mig-idle",
		VirtualMachineID: virtualMachineID,
		OriginalDesired:  vm.StateRunning,
		State:            sourceLocked,
		LastContactAt:    manager.now().Add(-sourceIdleTimeout - time.Minute),
	}
}

func TestExpireSourceReleasesAnIdlePreStopLock(t *testing.T) {
	manager, machines, _ := newMigrationManager(t)
	record := expirableSourceRecord(manager, "vm-1")
	record.TemporaryDiskLimitMiBps = 8
	if err := manager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if !manager.IsSourceLocked("vm-1") {
		t.Fatal("a live record must hold the VM")
	}
	if err := manager.ExpireSource(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}

	if manager.IsSourceLocked("vm-1") {
		t.Fatal("an expired record must release the VM")
	}
	if machines.refreshDiskCalls == 0 {
		t.Fatal("the transfer disk limit must be removed with the lock")
	}
	stored, err := manager.store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if stored.State != sourceExpired {
		t.Fatal("the tombstone must stay on disk")
	}
}

func TestExpireSourceKeepsAStoppedSource(t *testing.T) {
	manager, _, _ := newMigrationManager(t)
	record := expirableSourceRecord(manager, "vm-1")
	// The destination may have started a stopped source VM.
	record.State = sourceStopped
	if err := manager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if err := manager.ExpireSource(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}

	if !manager.IsSourceLocked("vm-1") {
		t.Fatal("a stopped source must stay locked")
	}
}

func TestExpireSourceKeepsARecentLock(t *testing.T) {
	manager, _, _ := newMigrationManager(t)
	record := expirableSourceRecord(manager, "vm-1")
	record.LastContactAt = manager.now()
	if err := manager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	expirable, err := manager.ExpiredSourceVirtualMachineIDs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(expirable) != 0 {
		t.Fatalf("expirable = %v, want none", expirable)
	}
}

func TestExpireSourceKeepsALockWithBytesInFlight(t *testing.T) {
	manager, _, _ := newMigrationManager(t)
	if err := manager.store.writeSource(expirableSourceRecord(manager, "vm-1")); err != nil {
		t.Fatal(err)
	}
	// An active stream keeps an otherwise idle lock alive.
	manager.sourceStreamsMutex.Lock()
	manager.sourceStreams["vm-1"] = &backgroundOperation{cancel: func() {}, done: make(chan struct{})}
	manager.sourceStreamsMutex.Unlock()

	expirable, err := manager.ExpiredSourceVirtualMachineIDs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(expirable) != 0 {
		t.Fatalf("expirable = %v, want none", expirable)
	}
}

func TestATombstoneRefusesItsOwnMigrationAndAcceptsANewOne(t *testing.T) {
	manager, machines, _ := newMigrationManager(t)
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateRunning)
	record := expirableSourceRecord(manager, "vm-1")
	record.State = sourceExpired
	if err := manager.store.writeSource(record); err != nil {
		t.Fatal(err)
	}

	if _, err := manager.LockSource(context.Background(), "mig-idle", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("resurrecting the expired migration = %v, want a conflict", err)
	}
	if _, err := manager.StopSource(context.Background(), "mig-idle", "vm-1", 0); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("stop against a tombstone = %v, want a conflict", err)
	}
	if err := manager.UnlockSource(context.Background(), "mig-idle", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("unlock against a tombstone = %v, want a conflict", err)
	}

	if _, err := manager.LockSource(context.Background(), "mig-new", "vm-1"); err != nil {
		t.Fatalf("a new migration must replace the tombstone: %v", err)
	}
	stored, err := manager.store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if stored.ID != "mig-new" || stored.State == sourceExpired {
		t.Fatalf("stored = %+v", stored)
	}
}

func TestLockSourceRejectsRescueAndPendingExit(t *testing.T) {
	manager, machines, _ := newMigrationManager(t)
	machines.create("vm-1", testSpecification())
	setObservedState(t, machines, "vm-1", vm.StateStopped)
	desired, err := machines.ReadDesired("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	desired.Specification.Rescue.Enabled = true
	desired.RescueGeneration = 1
	machines.desired["vm-1"] = desired
	if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("rescue migration: %v", err)
	}
	desired.Specification.Rescue.Enabled = false
	machines.desired["vm-1"] = desired
	if _, err := manager.LockSource(t.Context(), "mig-1", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("pending rescue exit migration: %v", err)
	}
}
