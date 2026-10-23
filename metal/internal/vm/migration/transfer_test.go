package migration

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestBeginIntervalReturnsARecordStoreError(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	if err := os.WriteFile(filepath.Join(machines.VirtualMachineRecordsDirectory(), "blocked"), []byte("not a directory"), 0o640); err != nil {
		t.Fatal(err)
	}
	record := destinationRecord{ID: "mig-1", VirtualMachineID: "blocked"}

	_, err := migrationManager.beginInterval(t.Context(), record, SourceSnapshot{Sequence: 1}, 0)
	if err == nil {
		t.Fatal("begin interval ignored the record store error")
	}
}

// writeCopyingDestination sets up a copying destination.
func writeCopyingDestination(t *testing.T, machines *fakeMigrationHost, sourceState vm.State) *migrationStore {
	t.Helper()
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record := destinationRecord{
		ID:                  "mig-1",
		VirtualMachineID:    "vm-1",
		Source:              "https://10.0.0.3:9000",
		State:               destinationCopying,
		SourceObservedState: sourceState,
		CopyStartedAt:       time.Now().UTC(),
		LastControlAt:       time.Now().UTC(),
	}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	machines.desired["vm-1"] = vm.DesiredRecord{ID: "vm-1", Specification: testSpecification()}
	return store
}

func TestDestinationStatusShowsBytesOfTheIntervalInFlight(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, _ := store.readDestination("vm-1")
	record.Intervals = []TransferProgress{{Sequence: 1, Completed: true, BytesTransferred: 10}, {Sequence: 2}}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	received := new(atomic.Int64)
	received.Store(4096)
	migrationManager.receivedBytes.Store("vm-1", received)

	progress, err := migrationManager.DestinationStatus(t.Context(), "mig-1")
	if err != nil {
		t.Fatal(err)
	}
	if progress.Intervals[0].BytesTransferred != 10 || progress.Intervals[1].BytesTransferred != 4096 {
		t.Fatalf("intervals = %+v", progress.Intervals)
	}
}

// seedDestinationVM writes reconstructed destination records in the original state.
func seedDestinationVM(t *testing.T, machines *fakeMigrationHost, desiredState vm.State) {
	t.Helper()
	machines.desired["vm-1"] = vm.DesiredRecord{
		ID: "vm-1", UserID: 1000, GroupID: 1000, State: desiredState,
		CreateFingerprint:       strings.Repeat("a", 64),
		Specification:           testSpecification(),
		Generation:              2,
		SpecificationGeneration: 1,
		RestartGeneration:       1,
	}
	setObservedState(t, machines, "vm-1", vm.StateUnknown)
}

func TestRunTransferCopiesAFullInterval(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	// One queued snapshot stops the loop after the full interval.
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 1000, GUID: "g"}}
	transfer.guid = "g"
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusRunning || record.State.phase() != PhaseCopying {
		t.Fatalf("record = %+v", record)
	}
	if len(record.Intervals) != 1 || !record.Intervals[0].Completed || record.Intervals[0].BytesTransferred != 1000 {
		t.Fatalf("intervals = %+v", record.Intervals)
	}
	if transfer.received != 1 {
		t.Fatalf("received count = %d", transfer.received)
	}
}

func TestRunTransferFailsOnGUIDMismatch(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 1000, GUID: "source-guid"}}
	transfer.guid = "different-guid"
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusFailed || record.Error == nil {
		t.Fatalf("record = %+v", record)
	}
}

func TestRunTransferRefusesAnUnrelatedDataset(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.datasetExists = true
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 1000, GUID: "g"}}
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusFailed {
		t.Fatalf("status = %s, want failed", record.State.status())
	}
	if transfer.received != 0 {
		t.Fatal("received a stream into an unrelated dataset")
	}
}

func TestRunTransferCutsOverAtTheIntervalLimit(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	for sequence := 1; sequence <= maxTransferIntervals; sequence++ {
		record.Intervals = append(record.Intervals, TransferProgress{Sequence: sequence, Completed: true})
	}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	// Fail source stop so the run remains in stopping.
	source.stopError = errors.New("stop refused")

	migrationManager.runTransfer(context.Background(), "vm-1")

	if source.nextCalls != 0 {
		t.Fatalf("asked the source past the interval limit: %d calls", source.nextCalls)
	}
	if source.stopCalls != 1 {
		t.Fatalf("stop calls = %d, want 1", source.stopCalls)
	}
	if source.stopReceived != maxTransferIntervals {
		t.Fatalf("stop acknowledged sequence = %d, want %d", source.stopReceived, maxTransferIntervals)
	}
	record, err = store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.phase() != PhaseStopping {
		t.Fatalf("phase = %s, want stopping", record.State.phase())
	}
}

func TestRunTransferThrottlesThenCutsOverToReady(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	migrationManager.finalDeltaMiB = 1
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "g"
	large := int64(64 * 1024 * 1024)
	source.nextQueue = []SourceSnapshot{
		{Sequence: 1, SizeBytes: large, GUID: "g"},
		{Sequence: 2, SizeBytes: large, GUID: "g"},
		{Sequence: 3, SizeBytes: 512, GUID: "g"},
	}
	source.stopSnapshot = SourceSnapshot{Sequence: 3, SizeBytes: 512, GUID: "g"}
	seedDestinationVM(t, machines, vm.StateRunning)
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusReady {
		t.Fatalf("status = %s (%+v)", record.State.status(), record.Error)
	}
	// The first and final intervals are unthrottled; the large delta uses 64 MiB/s.
	if want := []int{0, 64, 0}; !equalInts(source.streamMiBps, want) {
		t.Fatalf("throttle steps = %v", source.streamMiBps)
	}
	if source.stopCalls != 1 || machines.applyStateCalls != 1 {
		t.Fatalf("stop calls = %d, apply state calls = %d", source.stopCalls, machines.applyStateCalls)
	}
	if record.State != destinationReady {
		t.Fatalf("destination did not become ready: %+v", record)
	}
}

func TestRunTransferStopsANonRunningSourceFirst(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "g"
	source.stopSnapshot = SourceSnapshot{Sequence: 1, SizeBytes: 4096, GUID: "g"}
	seedDestinationVM(t, machines, vm.StateStopped)
	store := writeCopyingDestination(t, machines, vm.StateStopped)

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusReady {
		t.Fatalf("status = %s (%+v)", record.State.status(), record.Error)
	}
	if source.nextCalls != 0 || source.stopCalls != 1 {
		t.Fatalf("next calls = %d, stop calls = %d", source.nextCalls, source.stopCalls)
	}
	// A non-running source cuts over without a copy round and applies destination state once.
	if machines.applyStateCalls != 1 {
		t.Fatalf("apply state calls = %d, want 1", machines.applyStateCalls)
	}
	if len(record.Intervals) != 1 || !record.Intervals[0].Completed {
		t.Fatalf("intervals = %+v", record.Intervals)
	}
}

func TestRunTransferKeepsLockedOnDestinationStartFailure(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	transfer.guid = "g"
	source.stopSnapshot = SourceSnapshot{Sequence: 1, SizeBytes: 4096, GUID: "g"}
	seedDestinationVM(t, machines, vm.StateRunning)
	store := writeCopyingDestination(t, machines, vm.StateStopped)
	machines.applyStateError = errors.New("boot failed")

	migrationManager.runTransfer(context.Background(), "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State.status() != StatusFailed {
		t.Fatalf("status = %s, want failed", record.State.status())
	}
	// A post-stop failure must not unlock the source.
	if source.removeCalls != 0 {
		t.Fatalf("remove calls = %d, want the source kept locked", source.removeCalls)
	}
}

func TestAdvanceDestinationResumesACopyingTransfer(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 800, GUID: "g"}}
	transfer.guid = "g"
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	// Reconciliation resumes a copying migration.
	if err := migrationManager.AdvanceDestination(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	awaitTransfer(t, migrationManager, "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(record.Intervals) != 1 || !record.Intervals[0].Completed {
		t.Fatalf("intervals = %+v", record.Intervals)
	}
}

func TestStartTransferRunsOnceAndShutdownWaits(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	transfer := migrationManager.disks.(*fakeMigrationStorage)
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 500, GUID: "g"}}
	transfer.guid = "g"
	store := writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.StartTransfer("vm-1")
	migrationManager.StartTransfer("vm-1")
	awaitTransfer(t, migrationManager, "vm-1")

	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(record.Intervals) != 1 || !record.Intervals[0].Completed {
		t.Fatalf("intervals = %+v", record.Intervals)
	}
}

func TestCancelTransferStopsABlockedWorker(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	source.nextQueue = []SourceSnapshot{{Sequence: 1, SizeBytes: 1000, GUID: "g"}}
	source.streamHang = true
	writeCopyingDestination(t, machines, vm.StateRunning)

	migrationManager.StartTransfer("vm-1")
	// Cancellation returns after the blocked stream exits.
	if err := migrationManager.CancelTransfer(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestCancelTransferWithoutAWorkerIsANoOp(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	if err := migrationManager.CancelTransfer(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}
}

func equalInts(got, want []int) bool {
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

// The worker and the API both write the destination record. A worker write must keep
// an abort that arrived while the worker held an older copy.
func TestAWorkerWriteKeepsAnAbortThatArrivedFirst(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	stale, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.requestAbort(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	// Each of these once wrote the caller's copy, which had no abort on it.
	if _, err := migrationManager.beginInterval(t.Context(), stale, SourceSnapshot{Sequence: 1}, 0); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.enterStopping(t.Context(), stale); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.completeInterval(t.Context(), stale, 1, 2048, "guid-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.failTransfer(t.Context(), stale, "late failure"); !errors.Is(err, errTransferFailed) {
		t.Fatal(err)
	}

	current, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if current.State != destinationRemovingRuntime {
		t.Fatalf("state = %q, want %q", current.State, destinationRemovingRuntime)
	}
	if len(current.Intervals) != 1 || !current.Intervals[0].Completed || current.Intervals[0].FinishedAt.IsZero() {
		t.Fatalf("interval was not completed: %+v", current.Intervals)
	}
}

// A cancelled worker must still record an interval whose snapshot already landed.
func TestCompleteIntervalOutlivesTheWorkerContext(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	store := writeCopyingDestination(t, machines, vm.StateRunning)
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := migrationManager.beginInterval(t.Context(), record, SourceSnapshot{Sequence: 1}, 0); err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()

	if err := migrationManager.completeInterval(cancelled, record, 1, 2048, "guid-1"); err != nil {
		t.Fatal(err)
	}

	current, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(current.Intervals) != 1 || !current.Intervals[0].Completed {
		t.Fatalf("a cancelled worker lost the completed interval: %+v", current.Intervals)
	}
}

func TestWorkerCancellationIsNotATransferFailure(t *testing.T) {
	live := context.Background()
	stopped, cancel := context.WithCancel(context.Background())
	cancel()

	if isWorkerCancelled(live, errors.New("create destination network: no bridge")) {
		t.Fatal("a real failure on a live worker must fail the migration")
	}
	if isWorkerCancelled(live, context.Canceled) {
		t.Fatal("a cancelled step on a live worker must fail the migration")
	}
	if isWorkerCancelled(stopped, errors.New("no bridge")) {
		t.Fatal("a real failure must fail the migration even during a shutdown")
	}
	if !isWorkerCancelled(stopped, context.Canceled) {
		t.Fatal("a shutdown that cancels a step must leave the migration for the next pass")
	}
	if !isWorkerCancelled(stopped, fmt.Errorf("apply destination state: %w", context.Canceled)) {
		t.Fatal("a wrapped cancellation must also leave the migration for the next pass")
	}
}
