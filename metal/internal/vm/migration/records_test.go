package migration

import (
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/patches"
	"github.com/frappe/atlas/metal/internal/vm"
)

func newDestinationRecord() destinationRecord {
	return destinationRecord{
		ID:               "mig-1",
		VirtualMachineID: "vm-1",
		Source:           "https://10.0.0.3:9000",
		State:            destinationPreparing,
		CreatedAt:        time.Now().UTC(),
	}
}

func TestDestinationRecordRoundTrips(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	record := newDestinationRecord()
	record.Definition = &VirtualMachineDefinition{VirtualMachineID: "vm-1", Specification: vm.Specification{CPUMillicores: 2000, MemoryMiB: 2048}}

	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	got, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != "mig-1" || got.Definition == nil || got.Definition.Specification.MemoryMiB != 2048 {
		t.Fatalf("destination record = %+v", got)
	}
}

func TestAColdCopyReportsCopyingUntilTheFinalSnapshotArrives(t *testing.T) {
	record := newDestinationRecord()
	record.State = destinationStopping
	if phase := record.progress().Phase; phase != PhaseCopying {
		t.Fatalf("cold copy phase = %s, want %s", phase, PhaseCopying)
	}

	record.Intervals = []TransferProgress{{Sequence: 1, Completed: true}}
	if phase := record.progress().Phase; phase != PhaseStopping {
		t.Fatalf("live cutover phase = %s, want %s", phase, PhaseStopping)
	}
}

func TestMemoryProgressSurvivesReloadAndTerminalCleanup(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	record := newDestinationRecord()
	record.HasMemorySnapshot = true
	for _, step := range []struct {
		state   destinationState
		stopped bool
		failure bool
		want    string
	}{
		{destinationPreparing, false, false, "not_started"},
		{destinationCopying, false, false, "not_started"},
		{destinationStopping, false, false, "creating"},
		{destinationStopping, true, false, "transferring"},
		{destinationCreatingNetwork, true, false, "restoring"},
		{destinationApplyingState, true, false, "restoring"},
		{destinationReady, true, false, "completed"},
		{destinationFinishing, true, false, "completed"},
		{destinationFailed, true, true, "failed"},
		{destinationRemovingRuntime, true, true, "failed"},
		{destinationRestoringSource, true, false, "aborted"},
		{destinationCompleted, true, false, "completed"},
		{destinationAborted, true, false, "aborted"},
	} {
		record.State, record.SourceStopped = step.state, step.stopped
		record.Error = nil
		if step.failure {
			record.Error = &vm.OperationError{Code: "transfer_failed"}
		}
		if step.state.terminal() {
			record = terminalDestinationRecord(record, step.state, time.Now().UTC())
		}
		if err := store.writeDestination(record); err != nil {
			t.Fatal(err)
		}
		loaded, err := store.readDestination(record.VirtualMachineID)
		if err != nil {
			t.Fatal(err)
		}
		if got := loaded.progress().MemorySnapshotStatus; got != step.want {
			t.Fatalf("%s: snapshot status = %s, want %s", step.state, got, step.want)
		}
		loaded.HasMemorySnapshot = false
		if got := loaded.progress().MemorySnapshotStatus; got != "" {
			t.Fatalf("disk-only migration reported snapshot status %s", got)
		}
	}
}

func TestTerminalRecordNeedsNoPhase(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	record := newDestinationRecord()
	record.State = destinationCompleted
	record.FinishedAt = time.Now().UTC()
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	got, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatalf("a completed record with no phase must read back: %v", err)
	}
	if got.State != destinationCompleted {
		t.Fatalf("record = %+v", got)
	}
}

func TestDestinationRecordRequiresAState(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	record := newDestinationRecord()
	record.State = ""
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}
	if _, err := store.readDestination("vm-1"); err == nil {
		t.Fatal("a destination record with no state must be rejected")
	}
}

func TestTransferStateRoundTrips(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	startedAt := time.Now().UTC().Add(-42 * time.Second)
	finishedAt := time.Now().UTC()
	destination := newDestinationRecord()
	destination.State = destinationCopying
	destination.SourceObservedState = vm.StateRunning
	destination.CopyStartedAt = time.Now().UTC()
	destination.Intervals = []TransferProgress{
		{Sequence: 1, StartedAt: startedAt, FinishedAt: finishedAt, DurationSeconds: 42, BytesTransferred: 1024, TotalBytes: 1024, GUID: "g1", Completed: true},
		{Sequence: 2, BytesTransferred: 256, TotalBytes: 1024},
	}
	if err := store.writeDestination(destination); err != nil {
		t.Fatal(err)
	}
	source := sourceRecord{
		ID: "mig-1", VirtualMachineID: "vm-2",
		State: sourceLocked, Sequence: 2, AcknowledgedSequence: 1,
	}
	if err := store.writeSource(source); err != nil {
		t.Fatal(err)
	}

	gotDestination, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(gotDestination.Intervals) != 2 || !gotDestination.Intervals[0].Completed || !gotDestination.Intervals[0].FinishedAt.Equal(finishedAt) {
		t.Fatalf("destination = %+v", gotDestination)
	}
	gotSource, err := store.readSource("vm-2")
	if err != nil {
		t.Fatal(err)
	}
	if gotSource.Sequence != 2 || gotSource.AcknowledgedSequence != 1 {
		t.Fatalf("source = %+v", gotSource)
	}
}

func TestSourceRecordRoundTrips(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	record := sourceRecord{
		ID:               "mig-1",
		VirtualMachineID: "vm-1",
		OriginalDesired:  vm.StateRunning,
		State:            sourceLocked,
		LastContactAt:    time.Now().UTC(),
	}
	if err := store.writeSource(record); err != nil {
		t.Fatal(err)
	}
	got, err := store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if got.OriginalDesired != vm.StateRunning {
		t.Fatalf("source record = %+v", got)
	}
}

func TestMemoryMigrationPatchKeepsLegacyRecordsReadable(t *testing.T) {
	baseDirectory := t.TempDir()
	store := newMigrationStore(filepath.Join(baseDirectory, "machines"))
	source := sourceRecord{
		SchemaVersion: migrationSchemaVersion,
		ID:            "mig-1", VirtualMachineID: "vm-1",
		State: sourceLocked, OriginalDesired: vm.StateRunning,
		Sequence: 2, AcknowledgedSequence: 1,
	}
	destination := newDestinationRecord()
	destination.SchemaVersion = migrationSchemaVersion
	destination.VirtualMachineID = "vm-2"
	destination.State = destinationCopying
	destination.Intervals = []TransferProgress{{Sequence: 1, GUID: "g1", Completed: true}}
	if err := store.writeSource(source); err != nil {
		t.Fatal(err)
	}
	if err := store.writeDestination(destination); err != nil {
		t.Fatal(err)
	}

	if err := patches.Run(baseDirectory, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	gotSource, err := store.readSource("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotSource, source) {
		t.Fatalf("source record = %+v, want %+v", gotSource, source)
	}
	gotDestination, err := store.readDestination("vm-2")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotDestination, destination) {
		t.Fatalf("destination record = %+v, want %+v", gotDestination, destination)
	}
}

func TestFindDestinationResolvesTheMigrationID(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	if err := store.writeDestination(newDestinationRecord()); err != nil {
		t.Fatal(err)
	}
	virtualMachineID, record, err := store.findDestination("mig-1")
	if err != nil {
		t.Fatal(err)
	}
	if virtualMachineID != "vm-1" || record.ID != "mig-1" {
		t.Fatalf("found %s -> %+v", virtualMachineID, record)
	}
	if _, _, err := store.findDestination("mig-missing"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("find missing migration = %v, want ErrNotFound", err)
	}
}

func TestListVirtualMachineIDsFindsDestinationAndSource(t *testing.T) {
	store := newMigrationStore(t.TempDir())
	if err := store.writeDestination(newDestinationRecord()); err != nil {
		t.Fatal(err)
	}
	source := sourceRecord{ID: "mig-2", VirtualMachineID: "vm-2", State: sourceLocked}
	if err := store.writeSource(source); err != nil {
		t.Fatal(err)
	}
	ids, err := store.listVirtualMachineIDs()
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 2 || ids[0] != "vm-1" || ids[1] != "vm-2" {
		t.Fatalf("ids = %v", ids)
	}
}

func TestACorruptRecordAffectsOnlyItsMigration(t *testing.T) {
	directory := t.TempDir()
	migrationDirectory := filepath.Join(directory, "vm-1", migrationSubdirectory)
	if err := os.MkdirAll(migrationDirectory, 0o750); err != nil {
		t.Fatal(err)
	}
	// A record missing required fields cannot drive a migration.
	if err := os.WriteFile(filepath.Join(migrationDirectory, destinationFileName), []byte(`{"schema_version":2,"id":"mig-1"}`), 0o640); err != nil {
		t.Fatal(err)
	}
	store := newMigrationStore(directory)
	if err := store.writeDestination(destinationRecord{ID: "mig-2", VirtualMachineID: "vm-2", State: destinationCopying, CreatedAt: time.Now().UTC()}); err != nil {
		t.Fatal(err)
	}

	if virtualMachineID, _, err := store.findDestination("mig-2"); err != nil || virtualMachineID != "vm-2" {
		t.Fatalf("find a readable migration = %s, %v", virtualMachineID, err)
	}
	// The corrupt record can hold any other migration, so it is not reported absent.
	if _, _, err := store.findDestination("mig-1"); err == nil || errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("find beside a corrupt record = %v", err)
	}
	if !store.has(store.destinationPath("vm-1")) {
		t.Fatal("the corrupt migration record was removed")
	}
}

func TestReadDestinationRejectsAnUnknownField(t *testing.T) {
	directory := t.TempDir()
	migrationDirectory := filepath.Join(directory, "vm-1", migrationSubdirectory)
	if err := os.MkdirAll(migrationDirectory, 0o750); err != nil {
		t.Fatal(err)
	}
	body := `{"schema_version":2,"id":"mig-1","virtual_machine_id":"vm-1","source":"x","state":"preparing","created_at":"2026-01-01T00:00:00Z","caller":"gone"}`
	if err := os.WriteFile(filepath.Join(migrationDirectory, destinationFileName), []byte(body), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := newMigrationStore(directory).readDestination("vm-1"); err == nil {
		t.Fatal("an unknown field was accepted")
	}
}

func TestRemoveKeepsTheVirtualMachineRecords(t *testing.T) {
	directory := t.TempDir()
	store := newMigrationStore(directory)
	virtualMachineRecord := filepath.Join(directory, "vm-1", "config.json")
	if err := os.MkdirAll(filepath.Dir(virtualMachineRecord), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(virtualMachineRecord, []byte(`{}`), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := store.writeDestination(newDestinationRecord()); err != nil {
		t.Fatal(err)
	}

	if err := store.remove("vm-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(virtualMachineRecord); err != nil {
		t.Fatalf("VM record removed with the migration: %v", err)
	}
	ids, err := store.listVirtualMachineIDs()
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 0 {
		t.Fatalf("ids = %v", ids)
	}
}
