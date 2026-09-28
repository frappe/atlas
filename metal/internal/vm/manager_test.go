package vm

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

type fakeRuntime struct {
	state          State
	hasSavedState  bool
	starts         int
	stops          int
	saves          int
	restores       int
	restoresPaused int
	deletes        int
	pauses         int
	resumes        int
	removes        int
	coldStarts     int
	metadata       int
	diskRefresh    int
	diskLimitMiBps int
	inspectError   error
	restoreError   error
	saveError      error
	deleteError    error
	coldStartError error
	usage          Usage
	usageError     error
}

func (runtime *fakeRuntime) Inspect(context.Context, RuntimeMachine) (RuntimeStatus, error) {
	return RuntimeStatus{State: runtime.state, HasSavedState: runtime.hasSavedState}, runtime.inspectError
}

func (runtime *fakeRuntime) Start(context.Context, RuntimeMachine) error {
	runtime.starts++
	runtime.state = StateRunning
	return nil
}

func (runtime *fakeRuntime) ColdStart(context.Context, RuntimeMachine) error {
	runtime.coldStarts++
	if runtime.coldStartError != nil {
		return runtime.coldStartError
	}
	runtime.state = StateRunning
	runtime.hasSavedState = false
	return nil
}

func (runtime *fakeRuntime) Stop(context.Context, RuntimeMachine) error {
	runtime.stops++
	runtime.state = StateStopped
	runtime.hasSavedState = false
	return nil
}

func (runtime *fakeRuntime) SaveAndStop(context.Context, RuntimeMachine) error {
	runtime.saves++
	if runtime.saveError != nil {
		return runtime.saveError
	}
	runtime.state = StateStopped
	runtime.hasSavedState = true
	return nil
}

func (runtime *fakeRuntime) Restore(context.Context, RuntimeMachine) error {
	runtime.restores++
	if runtime.restoreError != nil {
		return runtime.restoreError
	}
	runtime.state = StateRunning
	runtime.hasSavedState = false
	return nil
}

func (runtime *fakeRuntime) RestorePaused(context.Context, RuntimeMachine) error {
	runtime.restoresPaused++
	if runtime.restoreError != nil {
		return runtime.restoreError
	}
	runtime.state = StatePaused
	runtime.hasSavedState = false
	return nil
}

func (runtime *fakeRuntime) DeleteSavedState(context.Context, RuntimeMachine) error {
	runtime.deletes++
	if runtime.deleteError != nil {
		return runtime.deleteError
	}
	runtime.hasSavedState = false
	return nil
}

func (runtime *fakeRuntime) Pause(context.Context, RuntimeMachine) error {
	runtime.pauses++
	runtime.state = StatePaused
	return nil
}

func (runtime *fakeRuntime) Resume(context.Context, RuntimeMachine) error {
	runtime.resumes++
	runtime.state = StateRunning
	return nil
}

func (runtime *fakeRuntime) Remove(context.Context, RuntimeMachine) error {
	runtime.removes++
	runtime.state = StateDestroyed
	return nil
}

func (runtime *fakeRuntime) RefreshMetadata(context.Context, RuntimeMachine) error {
	runtime.metadata++
	return nil
}

func (runtime *fakeRuntime) RefreshDisk(_ context.Context, machine RuntimeMachine) error {
	runtime.diskRefresh++
	runtime.diskLimitMiBps = machine.Specification.Disk.ThroughputMiBps
	return nil
}

func (runtime *fakeRuntime) ConnectSSH(context.Context, RuntimeMachine) (SSHConnection, error) {
	return nil, nil
}

func (runtime *fakeRuntime) Usage(context.Context, RuntimeMachine) (Usage, error) {
	return runtime.usage, runtime.usageError
}

type fakeNetwork struct {
	ensures     int
	releases    int
	lastRequest NetworkRequest
}

func (network *fakeNetwork) Ensure(_ context.Context, request NetworkRequest) (NetworkInterface, error) {
	network.ensures++
	network.lastRequest = request
	return NetworkInterface{MACAddress: "06:00:ac:10:00:02"}, nil
}

func (network *fakeNetwork) Release(context.Context, NetworkReleaseRequest) error {
	network.releases++
	return nil
}

type fakeStorage struct {
	resizes      int
	releases     int
	releaseError error
}

func (storage *fakeStorage) DiskUsage(context.Context, string) (DiskUsage, error) {
	return DiskUsage{}, nil
}

func (storage *fakeStorage) ResizeDisk(context.Context, string, int) error {
	storage.resizes++
	return nil
}

func (storage *fakeStorage) Release(context.Context, string) error {
	storage.releases++
	return storage.releaseError
}

type fakeSnapshots struct{}

func (fakeSnapshots) Stage(context.Context, SnapshotRequest) (StagedSnapshot, error) {
	return StagedSnapshot{}, nil
}

func newTestManager(t *testing.T) (*Manager, *fakeRuntime, *fakeNetwork, *fakeStorage) {
	t.Helper()
	runtime := &fakeRuntime{state: StateStopped}
	network := &fakeNetwork{}
	storage := &fakeStorage{}
	manager, err := NewManager(
		ManagerConfig{MachinesDirectory: t.TempDir(), UserIDRange: UserIDRange{Min: 1000, Max: 1010}},
		ManagerDependencies{
			Runtime:   runtime,
			Network:   network,
			Storage:   storage,
			Snapshots: fakeSnapshots{},
			Traffic:   &traffic.Monitor{},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	return manager, runtime, network, storage
}

func TestNewManagerAllowsTrafficMonitoringToBeDisabled(t *testing.T) {
	_, err := NewManager(
		ManagerConfig{MachinesDirectory: t.TempDir()},
		ManagerDependencies{Runtime: &fakeRuntime{}, Network: &fakeNetwork{}, Storage: &fakeStorage{}, Snapshots: fakeSnapshots{}},
	)
	if err != nil {
		t.Fatal(err)
	}
}

func testSpecification() Specification {
	return Specification{
		CPUMillicores: 2000,
		MemoryMiB:     2048,
		DiskMiB:       4096,
		Image: Image{
			Name:         "image-1",
			Architecture: "amd64",
			RootfsURL:    "https://example.com/rootfs?token=first",
			RootfsSHA256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			KernelURL:    "https://example.com/kernel?token=first",
			KernelSHA256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		},
		Network: NetworkConfiguration{WireGuardMeshIPv6: "fdaa::2", Routes: []Route{{Destination: "0.0.0.0/0", Via: RouteViaHost}}},
	}
}

func TestCreateFingerprintAcceptsChangedSignedURLs(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	specification := testSpecification()
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	specification.Image.RootfsURL = "https://example.com/rootfs?token=second"
	specification.Image.KernelURL = "https://example.com/kernel?token=second"
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatalf("create retry failed: %v", err)
	}
	record, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.Generation != 1 {
		t.Fatalf("generation = %d, want 1", record.Generation)
	}
	if record.Specification.Image.RootfsURL != specification.Image.RootfsURL {
		t.Fatal("signed URL was not refreshed")
	}
}

func TestCreateFingerprintRejectsDifferentFirstRequest(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	specification := testSpecification()
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	specification.MemoryMiB++
	if _, err := manager.Create(context.Background(), "machine-1", specification); !errors.Is(err, ErrConflict) {
		t.Fatalf("create error = %v, want conflict", err)
	}
}

func TestCreateFingerprintIncludesTheIdleTimeout(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	specification := testSpecification()
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	specification.SleepAfterIdleSeconds = 60
	if _, err := manager.Create(context.Background(), "machine-1", specification); !errors.Is(err, ErrConflict) {
		t.Fatalf("create error = %v, want conflict", err)
	}
}

func TestMutationGenerationChangesOnlyForNewValues(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	record, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.Generation != 2 {
		t.Fatalf("generation = %d, want 2", record.Generation)
	}
}

func TestSpecificationGenerationTracksShapeNotPower(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	base, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}

	// Power changes leave the specification generation unchanged.
	if err := manager.SetPowerState(context.Background(), "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), "machine-1", StateRunning); err != nil {
		t.Fatal(err)
	}
	afterPower, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if afterPower.SpecificationGeneration != base.SpecificationGeneration {
		t.Errorf("power change moved specification generation %d to %d", base.SpecificationGeneration, afterPower.SpecificationGeneration)
	}
	if afterPower.Generation == base.Generation {
		t.Error("power change did not raise the desired generation")
	}

	// A shape change raises the specification generation.
	configuration := testSpecification().Network
	configuration.PublicNetworkThroughputMiBps = 500
	if err := manager.SetNetwork(context.Background(), "machine-1", configuration); err != nil {
		t.Fatal(err)
	}
	afterShape, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if afterShape.SpecificationGeneration <= afterPower.SpecificationGeneration {
		t.Error("a network change did not raise the specification generation")
	}
}

func TestSetNetworkRejectsOversizedMetadataServiceDocument(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.Metadata = map[string]string{
		"large": strings.Repeat("x", MetadataServiceSizeLimitBytes-metadataServiceReserveBytes-600),
	}
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}

	network := specification.Network
	for index := range 40 {
		network.Routes = append(network.Routes, Route{
			Destination: fmt.Sprintf("2001:db8:%x::/48", index),
			Via:         RouteViaHost,
		})
	}
	if err := manager.SetNetwork(context.Background(), "machine-1", network); !errors.Is(err, ErrMetadataServiceTooLarge) {
		t.Fatalf("error = %v", err)
	}

	record, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(record.Specification.Network.Routes) != len(specification.Network.Routes) {
		t.Fatal("rejected network change was saved")
	}
}

func TestIdleTimeoutDoesNotChangeSpecificationGeneration(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	before, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.SetCompute(context.Background(), "machine-1", Compute{
		CPUMillicores: 2000, MemoryMiB: 2048, SleepAfterIdleSeconds: 60,
	}); err != nil {
		t.Fatal(err)
	}
	after, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if after.SpecificationGeneration != before.SpecificationGeneration || after.Generation <= before.Generation {
		t.Fatalf("before = %+v, after = %+v", before, after)
	}
}

func TestSetComputeRequestsRunningState(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetPowerState(context.Background(), "machine-1", StateStopped); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.SetCompute(context.Background(), "machine-1", Compute{CPUMillicores: 4000, MemoryMiB: 4096}); err != nil {
		t.Fatal(err)
	}
	record, err := manager.store.readDesired("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if record.State != StateRunning || record.Generation != 3 {
		t.Fatalf("desired record = %+v", record)
	}
}

func TestSetComputeRejectsAMissingVirtualMachine(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if err := manager.SetCompute(context.Background(), "missing", Compute{CPUMillicores: 1000, MemoryMiB: 1}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}

func TestSetComputeConflictsWithADestroyedVirtualMachine(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	compute := Compute{CPUMillicores: 1000, MemoryMiB: 1, SleepAfterIdleSeconds: 60}
	if err := manager.SetCompute(context.Background(), "machine-1", compute); !errors.Is(err, ErrConflict) {
		t.Fatalf("error = %v, want ErrConflict", err)
	}
}

func TestNewManagerRejectsUnknownAndTrailingRecordData(t *testing.T) {
	for _, data := range []string{
		`{"schema_version":1,"id":"machine-1","unknown":true}`,
		`{"schema_version":1,"id":"machine-1"} {}`,
		`{"id":"machine-1"}`,
	} {
		directory := t.TempDir()
		machineDirectory := filepath.Join(directory, "machine-1")
		if err := os.MkdirAll(machineDirectory, 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(machineDirectory, "config.json"), []byte(data), 0o640); err != nil {
			t.Fatal(err)
		}
		_, err := NewManager(
			ManagerConfig{MachinesDirectory: directory},
			ManagerDependencies{Runtime: &fakeRuntime{}, Network: &fakeNetwork{}, Storage: &fakeStorage{}, Snapshots: fakeSnapshots{}, Traffic: &traffic.Monitor{}},
		)
		if err == nil {
			t.Fatalf("NewManager accepted record %s", data)
		}
	}
}

// A create or a destroy leaves a directory with one record for a short time.
// Capacity reads must not fail for the whole host in that window.
func TestListSkipsIncompleteRecords(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	for _, identifier := range []string{"machine-1", "machine-2"} {
		if _, err := manager.Create(context.Background(), identifier, testSpecification()); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Remove(manager.store.observedPath("machine-2")); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(manager.store.directory, "machine-3"), 0o700); err != nil {
		t.Fatal(err)
	}

	information, err := manager.List(context.Background())
	if err != nil {
		t.Fatalf("List = %v", err)
	}
	if len(information) != 1 || information[0].ID != "machine-1" {
		t.Fatalf("information = %+v", information)
	}
}

// A record that is present but corrupt is a host fault, not a VM in flux.
func TestListReportsCorruptRecords(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(manager.store.observedPath("machine-1"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, err := manager.List(context.Background()); err == nil {
		t.Fatal("List accepted a corrupt record")
	}
}

func TestReconcileUsesRuntimeAndResourceDependencies(t *testing.T) {
	manager, runtime, network, storage := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.starts != 1 || runtime.metadata != 1 || runtime.diskRefresh != 1 {
		t.Fatalf("runtime calls = start %d, metadata %d, disk %d", runtime.starts, runtime.metadata, runtime.diskRefresh)
	}
	if network.ensures != 1 || storage.resizes != 1 {
		t.Fatalf("resource calls = network %d, storage %d", network.ensures, storage.resizes)
	}
	observed, err := manager.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.Generation != 1 || observed.State != StateRunning {
		t.Fatalf("observed record = %+v", observed)
	}
}

func TestCleanupProgressSurvivesRetry(t *testing.T) {
	manager, runtime, network, storage := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	storage.releaseError = errors.New("storage unavailable")
	if err := manager.Reconcile(context.Background(), "machine-1"); err == nil {
		t.Fatal("cleanup succeeded while storage failed")
	}
	storage.releaseError = nil
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.removes != 1 || network.releases != 1 || storage.releases != 2 {
		t.Fatalf("cleanup calls = runtime %d, network %d, storage %d", runtime.removes, network.releases, storage.releases)
	}
	if _, err := manager.Information(context.Background(), "machine-1"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("information error = %v, want not found", err)
	}
}

func TestRestartIntentSurvivesManagerRecreation(t *testing.T) {
	manager, runtime, network, storage := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.RequestRestart(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	recreated, err := NewManager(manager.configuration, ManagerDependencies{
		Runtime: runtime, Network: network, Storage: storage, Snapshots: fakeSnapshots{},
		Traffic: &traffic.Monitor{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := recreated.Reconcile(context.Background(), "machine-1"); err != nil {
		t.Fatal(err)
	}
	if runtime.stops != 1 || runtime.starts != 2 {
		t.Fatalf("restart calls = stops %d, starts %d", runtime.stops, runtime.starts)
	}
	observed, err := recreated.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.RestartGeneration != 1 {
		t.Fatalf("restart generation = %d, want 1", observed.RestartGeneration)
	}
}

func TestInspectFailureStoresSafeAndLocalErrors(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	runtime.inspectError = errors.New("socket /private/path failed")
	if err := manager.Reconcile(context.Background(), "machine-1"); err == nil {
		t.Fatal("reconcile succeeded while inspection failed")
	}
	observed, err := manager.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.State != StateUnknown || observed.Error == nil {
		t.Fatalf("observed record = %+v", observed)
	}
	if observed.Error.Message != "inspect operation failed" {
		t.Fatalf("safe message = %q", observed.Error.Message)
	}
	if observed.Error.LocalDetail != "socket /private/path failed" {
		t.Fatalf("local detail = %q", observed.Error.LocalDetail)
	}
	information, err := manager.Information(context.Background(), "machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if information.Error == nil || information.Error.Message != observed.Error.Message {
		t.Fatalf("public error = %+v", information.Error)
	}
}

func TestCreateSnapshotRejectsUnknownRuntimeState(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	runtime.state = StateUnknown
	if _, err := manager.CreateSnapshot(context.Background(), "machine-1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("snapshot error = %v, want conflict", err)
	}
	observed, err := manager.store.readObserved("machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if observed.State != StateUnknown || observed.Phase != "" || observed.OperationID != "" {
		t.Fatalf("observed record = %+v", observed)
	}
}

// The runtime makes a machine directory for a temporary VM. A directory that
// stays behind holds no records, so it stops every later start and list.
func TestRunTemporaryRemovesMachineDirectory(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	identifier := "warm-529e5118abe6"
	run := func(RuntimeMachine) error {
		return os.MkdirAll(filepath.Join(manager.store.directory, identifier, "firecracker"), 0o700)
	}
	if err := manager.RunTemporary(context.Background(), identifier, testSpecification(), run); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(filepath.Join(manager.store.directory, identifier)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("machine directory stat = %v, want not exist", err)
	}
}

// A host that stops during a temporary build keeps the directory. metald must
// still start, because a directory without a desired record is not a VM.
func TestNewManagerStartsWithLeftoverTemporaryDirectory(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	leftover := filepath.Join(manager.store.directory, "warm-529e5118abe6", "firecracker")
	if err := os.MkdirAll(leftover, 0o700); err != nil {
		t.Fatal(err)
	}

	if err := manager.store.validateAll(); err != nil {
		t.Fatalf("validateAll = %v", err)
	}
	identifiers, err := manager.store.listIDs()
	if err != nil {
		t.Fatal(err)
	}
	if len(identifiers) != 1 || identifiers[0] != "machine-1" {
		t.Fatalf("identifiers = %v", identifiers)
	}
}

func TestRunTemporaryRejectsInvalidIdentifier(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	err := manager.RunTemporary(context.Background(), "../machine-1", testSpecification(), func(RuntimeMachine) error {
		return nil
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("temporary machine error = %v, want conflict", err)
	}
}

func TestInformationStopsRetryingAPairThatStaysTorn(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}

	if err := os.Remove(manager.store.observedPath("machine-1")); err != nil {
		t.Fatal(err)
	}

	_, err := manager.Information(context.Background(), "machine-1")
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound once the retries give up, got %v", err)
	}
}

func TestInformationReadsAWholePair(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(context.Background(), "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}

	information, err := manager.Information(context.Background(), "machine-1")
	if err != nil {
		t.Fatal(err)
	}
	if information.ID != "machine-1" {
		t.Fatalf("want machine-1, got %q", information.ID)
	}
}

func TestATornRemovalIsOnlyOneMissingRecord(t *testing.T) {
	if !isTornRemoval(fmt.Errorf("read: %w", ErrNotFound), nil) {
		t.Fatal("a missing desired record with an observed record present is torn")
	}
	if !isTornRemoval(nil, fmt.Errorf("read: %w", ErrNotFound)) {
		t.Fatal("a missing observed record with a desired record present is torn")
	}
	if isTornRemoval(fmt.Errorf("read: %w", ErrNotFound), fmt.Errorf("read: %w", ErrNotFound)) {
		t.Fatal("both records missing is a completed removal, not a torn read")
	}
	if isTornRemoval(errors.New("permission denied"), nil) {
		t.Fatal("an unrelated failure is not a torn read")
	}
}

func TestStartWakesASleepingVirtualMachine(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.SleepAfterIdleSeconds = 60
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}
	runtime.hasSavedState = true

	if err := manager.SetPowerState(context.Background(), "machine-1", StateRunning); err != nil {
		t.Fatal(err)
	}
	if runtime.restores != 1 || runtime.state != StateRunning {
		t.Fatalf("restores = %d, state = %s, want one restore to running", runtime.restores, runtime.state)
	}
}

func TestStartLeavesAStoppedVirtualMachineWithoutSavedStateToReconcile(t *testing.T) {
	manager, runtime, _, _ := newTestManager(t)
	specification := testSpecification()
	specification.SleepAfterIdleSeconds = 60
	if _, err := manager.Create(context.Background(), "machine-1", specification); err != nil {
		t.Fatal(err)
	}

	if err := manager.SetPowerState(context.Background(), "machine-1", StateRunning); err != nil {
		t.Fatal(err)
	}
	if runtime.restores != 0 {
		t.Fatalf("restores = %d, want 0", runtime.restores)
	}
}
