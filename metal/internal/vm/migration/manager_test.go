package migration

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// fakeMigrationHost implements migrationHost with in-memory records and call counters.
type fakeMigrationHost struct {
	dir      string
	desired  map[string]vm.DesiredRecord
	observed map[string]vm.ObservedRecord

	nextUserID      uint32
	limitDiskCap    int
	allocationMutex sync.Mutex
	operationLocks  vm.KeyedLocks
	operationCalls  chan string

	normalizeCalls      int
	removeNetworkCalls  int
	ensureNetworkCalls  int
	applyStateCalls     int
	restoreCalls        int
	removeRuntimeCalls  int
	releaseStorageCalls int
	refreshDiskCalls    int
	limitDiskCalls      int
	limitDiskValue      int

	applyStateError error
}

func newFakeMigrationHost(t *testing.T) *fakeMigrationHost {
	t.Helper()
	return &fakeMigrationHost{
		dir:        t.TempDir(),
		desired:    make(map[string]vm.DesiredRecord),
		observed:   make(map[string]vm.ObservedRecord),
		nextUserID: 100001,
	}
}

// create seeds a running source VM with a valid create fingerprint.
func (f *fakeMigrationHost) create(id string, specification vm.Specification) {
	f.desired[id] = vm.DesiredRecord{
		ID:                id,
		UserID:            1000,
		GroupID:           1000,
		CreateFingerprint: strings.Repeat("a", 64),
		Generation:        1,
		State:             vm.StateRunning,
		Specification:     specification,
	}
	f.observed[id] = vm.ObservedRecord{State: vm.StateUnknown, UpdatedAt: time.Now().UTC()}
}

func (f *fakeMigrationHost) ReadDesired(virtualMachineID string) (vm.DesiredRecord, error) {
	record, ok := f.desired[virtualMachineID]
	if !ok {
		return vm.DesiredRecord{}, vm.ErrNotFound
	}
	return record, nil
}

func (f *fakeMigrationHost) ReadObserved(virtualMachineID string) (vm.ObservedRecord, error) {
	record, ok := f.observed[virtualMachineID]
	if !ok {
		return vm.ObservedRecord{}, vm.ErrNotFound
	}
	return record, nil
}

func (f *fakeMigrationHost) WriteDesired(record vm.DesiredRecord) error {
	f.desired[record.ID] = record
	return nil
}

func (f *fakeMigrationHost) WriteObserved(virtualMachineID string, record vm.ObservedRecord) error {
	f.observed[virtualMachineID] = record
	return nil
}

func (f *fakeMigrationHost) RemoveVirtualMachineRecords(virtualMachineID string) error {
	delete(f.desired, virtualMachineID)
	delete(f.observed, virtualMachineID)
	return nil
}

func (f *fakeMigrationHost) DesiredPath(virtualMachineID string) string {
	return filepath.Join(f.dir, virtualMachineID, "config.json")
}

func (f *fakeMigrationHost) ObservedPath(virtualMachineID string) string {
	return filepath.Join(f.dir, virtualMachineID, "status.json")
}

func (f *fakeMigrationHost) AllocateUserID() (uint32, error) {
	userID := f.nextUserID
	f.nextUserID++
	return userID, nil
}

func (f *fakeMigrationHost) LockUserIDAllocation() func() {
	f.allocationMutex.Lock()
	return f.allocationMutex.Unlock
}

func (f *fakeMigrationHost) LockOperation(ctx context.Context, virtualMachineID string) (func(), error) {
	if f.operationCalls != nil {
		f.operationCalls <- virtualMachineID
	}
	return f.operationLocks.Lock(ctx, virtualMachineID)
}

func (f *fakeMigrationHost) ReleaseStorage(context.Context, string) error {
	f.releaseStorageCalls++
	return nil
}

func (f *fakeMigrationHost) VirtualMachineRecordsDirectory() string { return f.dir }

func (f *fakeMigrationHost) NormalizeSourceToStopped(context.Context, string) error {
	f.normalizeCalls++
	return nil
}

func (f *fakeMigrationHost) RemoveMigrationNetwork(context.Context, string) error {
	f.removeNetworkCalls++
	return nil
}

func (f *fakeMigrationHost) EnsureMigrationNetwork(context.Context, string) error {
	f.ensureNetworkCalls++
	return nil
}

func (f *fakeMigrationHost) ApplyMigratedDestinationState(context.Context, string) error {
	f.applyStateCalls++
	return f.applyStateError
}

func (f *fakeMigrationHost) RestoreRuntimeState(context.Context, string, vm.State) error {
	f.restoreCalls++
	return nil
}

func (f *fakeMigrationHost) RemoveMigratedRuntime(context.Context, string) error {
	f.removeRuntimeCalls++
	return nil
}

func (f *fakeMigrationHost) RefreshSourceDisk(context.Context, string) error {
	f.refreshDiskCalls++
	return nil
}

func (f *fakeMigrationHost) LimitSourceDisk(_ context.Context, _ string, throughputMiBps int) (int, error) {
	f.limitDiskCalls++
	if f.limitDiskCap > 0 && throughputMiBps > f.limitDiskCap {
		throughputMiBps = f.limitDiskCap
	}
	f.limitDiskValue = throughputMiBps
	return throughputMiBps, nil
}

func testSpecification() vm.Specification {
	return vm.Specification{
		CPUMillicores: 2000,
		MemoryMiB:     2048,
		DiskMiB:       4096,
	}
}

type fakeMigrationStorage struct {
	created       []string
	removed       []string
	sent          []string
	guid          string
	sizeBytes     int64
	datasetExists bool
	resumeToken   string
	received      int
	aborts        int
	sendErr       error
	receiveErr    error
	abortErr      error
	sendHang      bool
	sendStarted   chan struct{}
	serverRelease <-chan struct{}
}

func (f *fakeMigrationStorage) CreateSnapshot(_ context.Context, _, name string) error {
	f.created = append(f.created, name)
	return nil
}

func (f *fakeMigrationStorage) RemoveSnapshot(_ context.Context, _, name string) error {
	f.removed = append(f.removed, name)
	return nil
}

func (f *fakeMigrationStorage) SnapshotGUID(_ context.Context, _, _ string) (string, error) {
	return f.guid, nil
}

func (f *fakeMigrationStorage) EstimateStreamBytes(_ context.Context, _, _, _ string) (int64, error) {
	return f.sizeBytes, nil
}

func (f *fakeMigrationStorage) DestinationDatasetExists(_ context.Context, _ string) (bool, error) {
	return f.datasetExists, nil
}

func (f *fakeMigrationStorage) ReceiveResumeToken(_ context.Context, _ string) (string, error) {
	return f.resumeToken, nil
}

type fakeSourceStream struct {
	done chan error
}

func (stream *fakeSourceStream) Wait() error { return <-stream.done }

func (f *fakeMigrationStorage) StartSnapshotServer(ctx context.Context, _, name, base, token string) (storage.SourceStream, error) {
	f.sent = append(f.sent, name+"|"+base+"|"+token)
	if f.sendStarted != nil {
		f.sendStarted <- struct{}{}
	}
	if f.serverRelease != nil {
		<-f.serverRelease
	}
	stream := &fakeSourceStream{done: make(chan error, 1)}
	go func() {
		if f.sendHang {
			<-ctx.Done()
			stream.done <- ctx.Err()
			return
		}
		stream.done <- f.sendErr
	}()
	return stream, nil
}

func (f *fakeMigrationStorage) ReceiveSnapshotTLS(_ context.Context, _ string, _ string) error {
	f.received++
	return f.receiveErr
}

func (f *fakeMigrationStorage) AbortReceive(_ context.Context, _ string) error {
	f.aborts++
	return f.abortErr
}

type fakeSourceOperations struct {
	prepareCalls      int
	removeCalls       int
	removeError       error
	prepareDefinition VirtualMachineDefinition
	prepareState      vm.State
	prepareError      error
	nextSnapshot      SourceSnapshot
	nextQueue         []SourceSnapshot
	nextError         error
	nextCalls         int
	nextSequences     []int
	streamError       error
	streamHang        bool
	streamMiBps       []int
	stopSnapshot      SourceSnapshot
	stopError         error
	stopCalls         int
	stopReceived      int
	startCalls        int
	startError        error
	finishCalls       int
	finishError       error
}

func (c *fakeSourceOperations) PrepareSource(context.Context, string, string, string) (VirtualMachineDefinition, vm.State, error) {
	c.prepareCalls++
	return c.prepareDefinition, c.prepareState, c.prepareError
}

func (c *fakeSourceOperations) NextSnapshot(_ context.Context, _, _, _ string, receivedSequence int) (SourceSnapshot, error) {
	c.nextSequences = append(c.nextSequences, receivedSequence)
	index := c.nextCalls
	c.nextCalls++
	if c.nextError != nil {
		return SourceSnapshot{}, c.nextError
	}
	if index < len(c.nextQueue) {
		return c.nextQueue[index], nil
	}
	if len(c.nextQueue) > 0 {
		return SourceSnapshot{}, errors.New("no more snapshots")
	}
	return c.nextSnapshot, nil
}

func (c *fakeSourceOperations) StartSnapshotStream(ctx context.Context, _, _, _ string, _ int, _ string, throughputMiBps int) error {
	c.streamMiBps = append(c.streamMiBps, throughputMiBps)
	if c.streamHang {
		<-ctx.Done()
		return ctx.Err()
	}
	return c.streamError
}

func (c *fakeSourceOperations) StopSource(_ context.Context, _, _, _ string, receivedSequence int) (SourceSnapshot, error) {
	c.stopCalls++
	c.stopReceived = receivedSequence
	return c.stopSnapshot, c.stopError
}

func (c *fakeSourceOperations) StartSource(context.Context, string, string, string) error {
	c.startCalls++
	return c.startError
}

func (c *fakeSourceOperations) FinishSource(context.Context, string, string, string) error {
	c.finishCalls++
	return c.finishError
}

func (c *fakeSourceOperations) RemoveSource(context.Context, string, string, string) error {
	c.removeCalls++
	return c.removeError
}

func ampleCapacity(context.Context) (AvailableCapacity, error) {
	return AvailableCapacity{MemoryMiB: 262144, StorageMiB: 4194304}, nil
}

// awaitTransfer waits for a worker to end without cancelling it.
func awaitTransfer(t *testing.T, manager *Manager, virtualMachineID string) {
	t.Helper()
	manager.transfersMutex.Lock()
	handle := manager.transfers[virtualMachineID]
	manager.transfersMutex.Unlock()
	if handle == nil {
		return
	}
	select {
	case <-handle.done:
	case <-time.After(10 * time.Second):
		t.Fatal("the transfer worker did not finish")
	}
}

func newMigrationManager(t *testing.T) (*Manager, *fakeMigrationHost, *fakeSourceOperations) {
	t.Helper()
	machines := newFakeMigrationHost(t)
	source := &fakeSourceOperations{}
	migrationManager, err := newManager(machines, source, &fakeMigrationStorage{}, ampleCapacity, 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	return migrationManager, machines, source
}

func TestNewManagerRejectsACorruptRecord(t *testing.T) {
	machines := newFakeMigrationHost(t)
	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	migrationDirectory := store.migrationDirectory("vm-1")
	if err := os.MkdirAll(migrationDirectory, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(migrationDirectory, sourceFileName), []byte(`{"schema_version":2,"id":"mig-1"}`), 0o640); err != nil {
		t.Fatal(err)
	}

	if _, err := newManager(machines, &fakeSourceOperations{}, &fakeMigrationStorage{}, ampleCapacity, 0, nil); err == nil {
		t.Fatal("manager started with a corrupt record")
	}
	if !store.has(store.sourcePath("vm-1")) {
		t.Fatal("manager removed the corrupt source record")
	}
}

func TestCreateDestinationReservesAndAcceptsARetry(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	ctx := context.Background()

	record, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil)
	if err != nil {
		t.Fatal(err)
	}
	if record.Status != StatusRunning || record.Phase != PhasePreparing {
		t.Fatalf("record = %+v", record)
	}
	if !migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("VM ID was not reserved")
	}

	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatalf("idempotent retry = %v", err)
	}
}

func TestCreateDestinationSerializesOneMigrationID(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	start := make(chan struct{})
	results := make(chan error, 2)

	for _, virtualMachineID := range []string{"vm-1", "vm-2"} {
		go func() {
			<-start
			_, err := migrationManager.CreateDestination(t.Context(), "mig-1", virtualMachineID, "https://10.0.0.3:9000", nil)
			results <- err
		}()
	}
	close(start)

	succeeded, conflicted := 0, 0
	for range 2 {
		switch err := <-results; {
		case err == nil:
			succeeded++
		case errors.Is(err, vm.ErrConflict):
			conflicted++
		default:
			t.Fatalf("create destination = %v", err)
		}
	}
	if succeeded != 1 || conflicted != 1 {
		t.Fatalf("succeeded = %d, conflicted = %d", succeeded, conflicted)
	}
}

func TestCreateDestinationRejectsAPlaintextSource(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)

	_, err := migrationManager.CreateDestination(context.Background(), "mig-1", "vm-1", "http://10.0.0.3:9001", nil)
	if !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("plaintext source = %v, want ErrConflict", err)
	}
}

func TestCreateDestinationRejectsChangedValues(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatal(err)
	}

	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.9:9000", nil); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("changed source = %v, want ErrConflict", err)
	}
	if _, err := migrationManager.CreateDestination(ctx, "mig-2", "vm-1", "https://10.0.0.3:9000", nil); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("changed migration ID = %v, want ErrConflict", err)
	}
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-2", "https://10.0.0.3:9000", nil); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("reused migration ID for another VM = %v, want ErrConflict", err)
	}
}

func TestCreateDestinationRejectsALiveVirtualMachineID(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	machines.create("vm-1", testSpecification())

	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("reserve a live VM ID = %v, want ErrConflict", err)
	}
}

func TestDestinationStatusResolvesTheMigrationID(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatal(err)
	}

	record, err := migrationManager.DestinationStatus(ctx, "mig-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.VirtualMachineID != "vm-1" {
		t.Fatalf("status = %+v", record)
	}
	if _, err := migrationManager.DestinationStatus(ctx, "mig-missing"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("missing status = %v, want ErrNotFound", err)
	}
}

func TestAbortDestinationUnlocksTheSourceAndClearsTheReservation(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AbortDestination(ctx, "mig-1"); err != nil {
		t.Fatal(err)
	}
	// Let the worker roll back, then wait for it.
	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	awaitTransfer(t, migrationManager, "vm-1")
	if source.removeCalls != 1 {
		t.Fatalf("RemoveSource calls = %d, want 1", source.removeCalls)
	}
	if migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("reservation still present after abort")
	}
}

func TestAbortDestinationKeepsRecordsWhenRollbackFails(t *testing.T) {
	migrationManager, _, source := newMigrationManager(t)
	source.removeError = errors.New("source unreachable")
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.AbortDestination(ctx, "mig-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.AdvanceDestination(ctx, "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := migrationManager.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if !migrationManager.IsDestinationReserved("vm-1") {
		t.Fatal("reservation was cleared despite a rollback failure")
	}
}

func TestUnlockSourceStopsAnInFlightStream(t *testing.T) {
	machines := newFakeMigrationHost(t)
	transfer := &fakeMigrationStorage{sendHang: true, sendStarted: make(chan struct{}, 1)}
	migrationManager, err := newManager(machines, &fakeSourceOperations{}, transfer, ampleCapacity, 0, nil)
	if err != nil {
		t.Fatal(err)
	}

	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	if err := store.writeSource(sourceRecord{ID: "mig-1", VirtualMachineID: "vm-1", State: sourceLocked, Sequence: 1}); err != nil {
		t.Fatal(err)
	}

	if err := migrationManager.StartSourceStream(context.Background(), "mig-1", "vm-1", 1, "", 0); err != nil {
		t.Fatal(err)
	}

	// Wait until the stream holds the snapshot, like a destination that stalled.
	select {
	case <-transfer.sendStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("the source stream did not start")
	}

	if err := migrationManager.UnlockSource(context.Background(), "mig-1", "vm-1"); err != nil {
		t.Fatalf("unlock with an in-flight stream = %v", err)
	}

	if len(transfer.removed) != 1 || transfer.removed[0] != "migration-mig-1-1" {
		t.Fatalf("removed snapshots = %v, want [migration-mig-1-1]", transfer.removed)
	}
	if store.has(store.sourcePath("vm-1")) {
		t.Fatal("the source record was not removed after unlock")
	}
}

func TestDestinationReservationsCountOnlyMigrationsWithConfig(t *testing.T) {
	migrationManager, machines, _ := newMigrationManager(t)
	ctx := context.Background()
	if _, err := migrationManager.CreateDestination(ctx, "mig-1", "vm-1", "https://10.0.0.3:9000", nil); err != nil {
		t.Fatal(err)
	}

	reservations, err := migrationManager.DestinationReservations(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(reservations) != 0 {
		t.Fatalf("preparing migration reserved capacity: %+v", reservations)
	}

	store := newMigrationStore(machines.VirtualMachineRecordsDirectory())
	record, err := store.readDestination("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	record.State = destinationCopying
	record.Definition = &VirtualMachineDefinition{Specification: vm.Specification{CPUMillicores: 3000, MemoryMiB: 3072, DiskMiB: 8192}}
	if err := store.writeDestination(record); err != nil {
		t.Fatal(err)
	}

	reservations, err = migrationManager.DestinationReservations(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(reservations) != 1 || reservations[0].CPUMillicores != 3000 || reservations[0].MemoryMiB != 3072 {
		t.Fatalf("reservations = %+v", reservations)
	}
}
