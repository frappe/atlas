package api

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/frappe/atlas/metal/internal/vm/migration"
)

type stubMigrationManager struct {
	record            migration.DestinationProgress
	createErr         error
	statusErr         error
	abortErr          error
	sourceDescription migration.SourceDescription
	lockErr           error
	unlockErr         error
	snapshot          migration.SourceSnapshot
	snapshotErr       error
	createArgs        []string
	createResize      *migration.Resize
	lockArgs          []string
	unlockArgs        []string
	snapshotArgs      []string
	streamArgs        []string
	streamSequence    int
	streamResume      string
	streamThroughput  int
	abortedID         string
	finishedID        string
	finishErr         error
	receivedSequence  int
	stopReceived      int
	stopArgs          []string
	stopErr           error
	startArgs         []string
	startErr          error
	destroyArgs       []string
	destroyErr        error
}

func (m *stubMigrationManager) CreateDestination(_ context.Context, migrationID, virtualMachineID, source string, resize *migration.Resize) (migration.DestinationProgress, error) {
	m.createArgs = []string{migrationID, virtualMachineID, source}
	m.createResize = resize
	return m.record, m.createErr
}

func (m *stubMigrationManager) DestinationStatus(context.Context, string) (migration.DestinationProgress, error) {
	return m.record, m.statusErr
}

func (m *stubMigrationManager) RequestFinish(_ context.Context, migrationID string) error {
	m.finishedID = migrationID
	return m.finishErr
}

func (m *stubMigrationManager) AbortDestination(_ context.Context, migrationID string) error {
	m.abortedID = migrationID
	return m.abortErr
}

func (m *stubMigrationManager) LockSource(_ context.Context, migrationID, virtualMachineID string) (migration.SourceDescription, error) {
	m.lockArgs = []string{migrationID, virtualMachineID}
	return m.sourceDescription, m.lockErr
}

func (m *stubMigrationManager) NextSourceSnapshot(_ context.Context, migrationID, virtualMachineID string, receivedSequence int) (migration.SourceSnapshot, error) {
	m.receivedSequence = receivedSequence
	m.snapshotArgs = []string{migrationID, virtualMachineID}
	return m.snapshot, m.snapshotErr
}

func (m *stubMigrationManager) StartSourceStream(_ context.Context, migrationID, virtualMachineID string, sequence int, resumeToken string, throughputMiBps int) error {
	m.streamArgs = []string{migrationID, virtualMachineID}
	m.streamSequence = sequence
	m.streamResume = resumeToken
	m.streamThroughput = throughputMiBps
	return m.startErr
}

func (m *stubMigrationManager) StopSource(_ context.Context, migrationID, virtualMachineID string, receivedSequence int) (migration.SourceSnapshot, error) {
	m.stopArgs = []string{migrationID, virtualMachineID}
	m.stopReceived = receivedSequence
	return m.snapshot, m.stopErr
}

func (m *stubMigrationManager) StartSourceRollback(_ context.Context, migrationID, virtualMachineID string) error {
	m.startArgs = []string{migrationID, virtualMachineID}
	return m.startErr
}

func (m *stubMigrationManager) DestroySource(_ context.Context, migrationID, virtualMachineID string) error {
	m.destroyArgs = []string{migrationID, virtualMachineID}
	return m.destroyErr
}

func (m *stubMigrationManager) UnlockSource(_ context.Context, migrationID, virtualMachineID string) error {
	m.unlockArgs = []string{migrationID, virtualMachineID}
	return m.unlockErr
}

func newMigrationTestServer(t *testing.T, migrations MigrationManager) http.Handler {
	return newMigrationTestServerWithWake(t, migrations, func() {})
}

func newMigrationTestServerWithWake(t *testing.T, migrations MigrationManager, wake func()) http.Handler {
	t.Helper()
	services := newFakeRuntimeServices()
	manager := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}, services: services}
	hostService, err := host.NewService(host.Dependencies{
		Mesh: services, WireGuard: &fakeWireGuardManager{}, Images: services,
		VirtualMachines: manager, Storage: fakeCapacityProvider{}, Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}
	server, err := New(Config{}, Dependencies{
		VirtualMachineManager: manager,
		MigrationManager:      migrations,
		SnapshotStore:         services,
		WakeReconciler:        wake,
		HostService:           hostService,
		SerialBroker:          stubSerialBroker{},
	})
	if err != nil {
		t.Fatal(err)
	}
	return server
}

func newCoordinationTestServer(t *testing.T, migrations MigrationManager) http.Handler {
	t.Helper()
	server, err := NewCoordination(nil, migrations)
	if err != nil {
		t.Fatal(err)
	}
	return server
}

func TestCreateMigrationDrivesTheDestination(t *testing.T) {
	stub := &stubMigrationManager{record: migration.DestinationProgress{ID: "mig-1", VirtualMachineID: "vm-1", Status: migration.StatusRunning, Phase: migration.PhasePreparing}}
	server := newMigrationTestServer(t, stub)

	body := `{"virtual_machine_id":"vm-1","source":"https://10.0.0.3:9000"}`
	recorder := do(t, server, http.MethodPut, "/v1/migrations/mig-1", body, http.StatusAccepted)

	if want := []string{"mig-1", "vm-1", "https://10.0.0.3:9000"}; !equalStrings(stub.createArgs, want) {
		t.Fatalf("create args = %v", stub.createArgs)
	}
	var response migrationResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.ID != "mig-1" || response.Status != "running" {
		t.Fatalf("response = %+v", response)
	}
}

func TestCreateMigrationPassesTheResize(t *testing.T) {
	stub := &stubMigrationManager{record: migration.DestinationProgress{ID: "mig-1", VirtualMachineID: "vm-1", Status: migration.StatusRunning}}
	server := newMigrationTestServer(t, stub)

	body := `{"virtual_machine_id":"vm-1","source":"https://10.0.0.3:9000","resize":{"cpu_millicores":2000,"memory_mib":4096,"disk_mib":20480}}`
	do(t, server, http.MethodPut, "/v1/migrations/mig-1", body, http.StatusAccepted)

	want := migration.Resize{CPUMillicores: 2000, MemoryMiB: 4096, DiskMiB: 20480}
	if stub.createResize == nil || *stub.createResize != want {
		t.Fatalf("resize = %+v, want %+v", stub.createResize, want)
	}
}

func TestCreateMigrationRejectsAnInvalidResize(t *testing.T) {
	server := newMigrationTestServer(t, &stubMigrationManager{})
	for _, resize := range []string{
		`{"cpu_millicores":50,"memory_mib":4096,"disk_mib":20480}`,
		`{"cpu_millicores":2000,"memory_mib":0,"disk_mib":20480}`,
		`{"cpu_millicores":2000,"memory_mib":4096,"disk_mib":0}`,
	} {
		body := `{"virtual_machine_id":"vm-1","source":"https://10.0.0.3:9000","resize":` + resize + `}`
		do(t, server, http.MethodPut, "/v1/migrations/mig-1", body, http.StatusBadRequest)
	}
}

func TestCreateMigrationRejectsAMissingField(t *testing.T) {
	server := newMigrationTestServer(t, &stubMigrationManager{})
	do(t, server, http.MethodPut, "/v1/migrations/mig-1", `{"virtual_machine_id":"vm-1"}`, http.StatusBadRequest)
}

func TestGetAndAbortMigration(t *testing.T) {
	stub := &stubMigrationManager{record: migration.DestinationProgress{ID: "mig-1", VirtualMachineID: "vm-1", Status: migration.StatusReady, Phase: migration.PhaseCopying}}
	wakeCalls := 0
	server := newMigrationTestServerWithWake(t, stub, func() { wakeCalls++ })

	recorder := do(t, server, http.MethodGet, "/v1/migrations/mig-1", "", http.StatusOK)
	var response migrationResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Status != "ready" {
		t.Fatalf("status = %s", response.Status)
	}

	do(t, server, http.MethodPost, "/v1/migrations/mig-1/abort", "", http.StatusAccepted)
	if stub.abortedID != "mig-1" {
		t.Fatalf("aborted = %q", stub.abortedID)
	}
	if wakeCalls != 1 {
		t.Fatalf("wake calls = %d, want 1", wakeCalls)
	}
}

func TestGetMigrationReportsTransferProgress(t *testing.T) {
	finishedAt := time.Date(2026, time.September, 20, 12, 0, 42, 0, time.UTC)
	stub := &stubMigrationManager{record: migration.DestinationProgress{
		ID: "mig-1", VirtualMachineID: "vm-1", Status: migration.StatusRunning, Phase: migration.PhaseCopying,
		Intervals: []migration.TransferProgress{
			{Sequence: 1, StartedAt: time.Date(2026, time.September, 20, 12, 0, 0, 0, time.UTC), FinishedAt: finishedAt, DurationSeconds: 42, BytesTransferred: 1024, TotalBytes: 1024, Completed: true},
			{Sequence: 2, BytesTransferred: 256, TotalBytes: 1024, ThroughputMiBps: 64},
		},
	}}
	server := newMigrationTestServer(t, stub)

	recorder := do(t, server, http.MethodGet, "/v1/migrations/mig-1", "", http.StatusOK)
	var response migrationResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.Transfers) != 2 {
		t.Fatalf("response = %+v", response)
	}
	transfer := response.Transfers[0]
	if transfer.Sequence != 1 || transfer.StartedAt.IsZero() || transfer.FinishedAt == nil {
		t.Fatalf("transfer timing = %+v", transfer)
	}
	if !transfer.FinishedAt.Equal(finishedAt) || !transfer.Completed || transfer.DurationSeconds != 42 {
		t.Fatalf("transfer completion = %+v", transfer)
	}
	if transfer.TransferredMiB != 1 || transfer.TotalMiB != 1 {
		t.Fatalf("transfer size = %+v", transfer)
	}
	// The throttle step is visible per interval, so an operator sees it decrease.
	if response.Transfers[1].ThroughputMiBps != 64 {
		t.Fatalf("transfer 1 throughput = %d, want 64", response.Transfers[1].ThroughputMiBps)
	}
}

func TestPrepareMigrationSourceLocksTheSource(t *testing.T) {
	stub := &stubMigrationManager{sourceDescription: migration.SourceDescription{
		Definition:    migration.VirtualMachineDefinition{VirtualMachineID: "vm-00001"},
		ObservedState: vm.StateRunning,
	}}
	server := newCoordinationTestServer(t, stub)

	recorder := do(t, server, http.MethodPut, "/v1/migrations/mig-1/source?virtual_machine_id=vm-00001", "", http.StatusOK)

	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.lockArgs, want) {
		t.Fatalf("lock args = %v", stub.lockArgs)
	}
	var response migration.SourceDescription
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Definition.VirtualMachineID != "vm-00001" || response.ObservedState != vm.StateRunning {
		t.Fatalf("response = %+v", response)
	}
}

func TestPrepareMigrationSourceRequiresTheVirtualMachineID(t *testing.T) {
	server := newCoordinationTestServer(t, &stubMigrationManager{})

	do(t, server, http.MethodPut, "/v1/migrations/mig-1/source", "", http.StatusBadRequest)
}

func TestCreateMigrationSnapshotReturnsTheNextSnapshot(t *testing.T) {
	stub := &stubMigrationManager{snapshot: migration.SourceSnapshot{Sequence: 2, SizeBytes: 1048576, GUID: "g2"}}
	server := newCoordinationTestServer(t, stub)

	recorder := do(t, server, http.MethodPost, "/v1/migrations/mig-1/snapshot?virtual_machine_id=vm-00001", `{"received_sequence":1}`, http.StatusOK)

	if stub.receivedSequence != 1 {
		t.Fatalf("received sequence = %d", stub.receivedSequence)
	}
	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.snapshotArgs, want) {
		t.Fatalf("snapshot args = %v", stub.snapshotArgs)
	}
	var response migration.SourceSnapshot
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Sequence != 2 || response.SizeBytes != 1048576 || response.GUID != "g2" {
		t.Fatalf("response = %+v", response)
	}
}

func TestCreateMigrationSnapshotAcceptsNoBody(t *testing.T) {
	stub := &stubMigrationManager{snapshot: migration.SourceSnapshot{Sequence: 1}}
	server := newCoordinationTestServer(t, stub)

	do(t, server, http.MethodPost, "/v1/migrations/mig-1/snapshot?virtual_machine_id=vm-00001", "", http.StatusOK)
	if stub.receivedSequence != 0 {
		t.Fatalf("received %d, want 0", stub.receivedSequence)
	}
}

func TestSourceRoutesRejectANegativeAcknowledgement(t *testing.T) {
	server := newCoordinationTestServer(t, &stubMigrationManager{})
	for _, suffix := range []string{"snapshot", "stop"} {
		do(t, server, http.MethodPost, "/v1/migrations/mig-1/"+suffix+"?virtual_machine_id=vm-00001", `{"received_sequence":-1}`, http.StatusBadRequest)
	}
}

func TestStartMigrationStreamPassesTheTransferRequest(t *testing.T) {
	stub := &stubMigrationManager{}
	server := newCoordinationTestServer(t, stub)

	do(
		t,
		server,
		http.MethodPost,
		"/v1/migrations/mig-1/stream?virtual_machine_id=vm-00001",
		`{"sequence":2,"resume_token":"resume","throughput_mibps":64}`,
		http.StatusAccepted,
	)

	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.streamArgs, want) {
		t.Fatalf("stream args = %v", stub.streamArgs)
	}
	if stub.streamSequence != 2 || stub.streamResume != "resume" || stub.streamThroughput != 64 {
		t.Fatalf("stream request = sequence %d, resume %q, throughput %d", stub.streamSequence, stub.streamResume, stub.streamThroughput)
	}
}

func TestStopMigrationSourceReturnsFinalSnapshot(t *testing.T) {
	stub := &stubMigrationManager{snapshot: migration.SourceSnapshot{Sequence: 3, SizeBytes: 2048, GUID: "final"}}
	server := newCoordinationTestServer(t, stub)

	recorder := do(t, server, http.MethodPost, "/v1/migrations/mig-1/stop?virtual_machine_id=vm-00001", `{"received_sequence":2}`, http.StatusOK)

	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.stopArgs, want) {
		t.Fatalf("stop args = %v", stub.stopArgs)
	}
	if stub.stopReceived != 2 {
		t.Fatalf("stop received sequence = %d, want 2", stub.stopReceived)
	}
	var response migration.SourceSnapshot
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Sequence != 3 || response.SizeBytes != 2048 || response.GUID != "final" {
		t.Fatalf("response = %+v", response)
	}
}

func TestFinishMigrationRecordsTheRequest(t *testing.T) {
	stub := &stubMigrationManager{}
	server := newMigrationTestServer(t, stub)

	do(t, server, http.MethodPost, "/v1/migrations/mig-1/finish", "", http.StatusAccepted)
	if stub.finishedID != "mig-1" {
		t.Fatalf("finished id = %q", stub.finishedID)
	}
}

func TestStartMigrationSourceRestores(t *testing.T) {
	stub := &stubMigrationManager{}
	server := newCoordinationTestServer(t, stub)

	do(t, server, http.MethodPost, "/v1/migrations/mig-1/start?virtual_machine_id=vm-00001", "", http.StatusNoContent)
	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.startArgs, want) {
		t.Fatalf("start args = %v", stub.startArgs)
	}
}

func TestDestroyMigrationSource(t *testing.T) {
	stub := &stubMigrationManager{}
	server := newCoordinationTestServer(t, stub)

	do(t, server, http.MethodPost, "/v1/migrations/mig-1/destroy?virtual_machine_id=vm-00001", "", http.StatusNoContent)
	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.destroyArgs, want) {
		t.Fatalf("destroy args = %v", stub.destroyArgs)
	}
}

func TestDeleteMigrationSourceUnlocks(t *testing.T) {
	stub := &stubMigrationManager{}
	server := newCoordinationTestServer(t, stub)

	do(t, server, http.MethodDelete, "/v1/migrations/mig-1?virtual_machine_id=vm-00001", "", http.StatusNoContent)
	if want := []string{"mig-1", "vm-00001"}; !equalStrings(stub.unlockArgs, want) {
		t.Fatalf("unlock args = %v", stub.unlockArgs)
	}
}

func equalStrings(got, want []string) bool {
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
