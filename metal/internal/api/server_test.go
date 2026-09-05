package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeVM struct {
	info    vm.Info
	started bool
}

func (f *fakeVM) ID() string { return f.info.ID }
func (f *fakeVM) Start(context.Context) error {
	f.started = true
	f.info.State = vm.StateRunning
	return nil
}
func (f *fakeVM) Stop(context.Context) error                  { f.info.State = vm.StateStopped; return nil }
func (f *fakeVM) Destroy(context.Context) error               { return nil }
func (f *fakeVM) Wait(context.Context) (vm.ExitStatus, error) { return vm.ExitStatus{}, nil }
func (f *fakeVM) Info(context.Context) (vm.Info, error)       { return f.info, nil }
func (f *fakeVM) Pause(context.Context) error                 { f.info.State = vm.StatePaused; return nil }
func (f *fakeVM) Resume(context.Context) error                { f.info.State = vm.StateRunning; return nil }
func (f *fakeVM) ResizeDisk(_ context.Context, diskMiB int) error {
	if diskMiB < f.info.DiskMiB {
		return vm.ErrConflict
	}
	f.info.DiskMiB = diskMiB
	return nil
}

type fakeVirtualMachineDriver struct {
	virtualMachines map[string]*fakeVM
	listError       error
}

func (driver *fakeVirtualMachineDriver) Create(_ context.Context, id string, specification vm.Spec) (vm.VM, error) {
	if existing, found := driver.virtualMachines[id]; found {
		return existing, nil
	}

	virtualMachine := &fakeVM{info: vm.Info{
		ID:                            id,
		State:                         vm.StateUnknown,
		DesiredState:                  vm.StateRunning,
		VCPUs:                         specification.VCPUs,
		MemoryMiB:                     specification.MemoryMiB,
		DiskMiB:                       specification.DiskMiB,
		Image:                         specification.Image,
		SSHKeys:                       append([]string(nil), specification.SSHKeys...),
		MAC:                           "06:00:00:00:00:01",
		PublicIPv4:                    specification.Network.PublicIPv4,
		Egress:                        specification.Network.Egress,
		WireGuardMeshIPv6:             specification.Network.WireGuardMeshIPv6,
		PrivateNetworkThroughputMiBps: specification.Network.PrivateNetworkThroughputMiBps,
		PublicNetworkThroughputMiBps:  specification.Network.PublicNetworkThroughputMiBps,
	}}
	driver.virtualMachines[id] = virtualMachine

	return virtualMachine, nil
}

func (driver *fakeVirtualMachineDriver) Load(_ context.Context, id string) (vm.VM, error) {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return nil, vm.ErrNotFound
	}

	return virtualMachine, nil
}

func (driver *fakeVirtualMachineDriver) List(context.Context) ([]vm.VM, error) {
	if driver.listError != nil {
		return nil, driver.listError
	}

	virtualMachines := make([]vm.VM, 0, len(driver.virtualMachines))
	for _, virtualMachine := range driver.virtualMachines {
		virtualMachines = append(virtualMachines, virtualMachine)
	}

	return virtualMachines, nil
}

func (driver *fakeVirtualMachineDriver) SetDesiredState(_ context.Context, id string, state vm.State) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}

	virtualMachine.info.DesiredState = state
	return nil
}

func (driver *fakeVirtualMachineDriver) ReplaceSSHKeys(
	_ context.Context,
	id string,
	sshKeys []string,
) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.SSHKeys = append([]string(nil), sshKeys...)
	return nil
}

func (driver *fakeVirtualMachineDriver) ReplaceMetadata(
	_ context.Context,
	id string,
	metadata map[string]string,
) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.Metadata = maps.Clone(metadata)
	return nil
}

func (driver *fakeVirtualMachineDriver) UpdateNetwork(_ context.Context, id string, update vm.NetworkUpdate) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.Egress = update.Egress
	virtualMachine.info.PublicIPv4 = update.PublicIPv4
	virtualMachine.info.PrivateNetworkThroughputMiBps = update.PrivateNetworkThroughputMiBps
	virtualMachine.info.PublicNetworkThroughputMiBps = update.PublicNetworkThroughputMiBps
	return nil
}

func (virtualMachine *fakeVM) UpdateDiskLimits(_ context.Context, limits vm.Disk) error {
	virtualMachine.info.DiskThroughputMiBps = limits.ThroughputMiBps
	virtualMachine.info.DiskIOPS = limits.IOPS
	return nil
}

func (driver *fakeVirtualMachineDriver) ResizeCompute(_ context.Context, id string, virtualCPUCount, memoryMiB int) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	if virtualMachine.info.State != vm.StateStopped {
		return vm.ErrConflict
	}

	virtualMachine.info.VCPUs = virtualCPUCount
	virtualMachine.info.MemoryMiB = memoryMiB
	virtualMachine.info.DesiredState = vm.StateRunning
	return nil
}

func (driver *fakeVirtualMachineDriver) Reboot(_ context.Context, id string) error {
	virtualMachine, found := driver.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	if virtualMachine.info.DesiredState != vm.StateRunning {
		return vm.ErrConflict
	}
	virtualMachine.info.State = vm.StateRunning
	return nil
}

type fakeRuntimeServices struct {
	policies  []vm.ImageRef
	snapshots map[string]storage.StagedSnapshot
}

func newFakeRuntimeServices() *fakeRuntimeServices {
	return &fakeRuntimeServices{snapshots: make(map[string]storage.StagedSnapshot)}
}

func (services *fakeRuntimeServices) CreateSnapshot(
	_ context.Context,
	virtualMachineID string,
) (storage.StagedSnapshot, error) {
	snapshotID := "01900000-0000-7000-8000-000000000001"
	snapshot := storage.StagedSnapshot{
		ID:                     snapshotID,
		SourceVirtualMachineID: virtualMachineID,
		Rootfs:                 storage.ArtifactSize{SizeBytes: 1024},
		Kernel:                 storage.ArtifactSize{SizeBytes: 512},
	}
	services.snapshots[snapshotID] = snapshot
	return snapshot, nil
}

func (services *fakeRuntimeServices) StartUpload(
	_ context.Context,
	snapshotID string,
	_ storage.SnapshotUploadRequest,
) error {
	if _, found := services.snapshots[snapshotID]; !found {
		return storage.ErrNotFound
	}
	return nil
}

func (services *fakeRuntimeServices) UploadStatus(
	_ context.Context,
	snapshotID string,
) (storage.SnapshotUploadStatus, error) {
	if _, found := services.snapshots[snapshotID]; !found {
		return storage.SnapshotUploadStatus{}, storage.ErrNotFound
	}
	return storage.SnapshotUploadStatus{ID: snapshotID, State: storage.UploadStateUploading}, nil
}

func (services *fakeRuntimeServices) DeleteSnapshot(_ context.Context, snapshotID string) error {
	delete(services.snapshots, snapshotID)
	return nil
}

func (services *fakeRuntimeServices) SetImagePolicies(_ context.Context, images []vm.ImageRef) error {
	services.policies = append([]vm.ImageRef(nil), images...)
	return nil
}

type fakeWireGuardManager struct {
	peers []network.WireGuardPeer
}

func (manager *fakeWireGuardManager) Apply(_ context.Context, peers []network.WireGuardPeer) error {
	manager.peers = append([]network.WireGuardPeer(nil), peers...)
	return nil
}

type fakeMesh struct {
	privileged []string
}

func (mesh *fakeMesh) ApplyPrivilegedAddresses(_ context.Context, addresses []string) error {
	mesh.privileged = append([]string(nil), addresses...)
	return nil
}

type fakeCapacityProvider struct{}

func (fakeCapacityProvider) Capacity(context.Context) (storage.Capacity, error) {
	return storage.Capacity{TotalMiB: 1000, AvailableMiB: 750}, nil
}

const (
	testToken     = "test-token"
	testTokenHash = "4c5dc9b7708905f77f5e5d16316b5dfb425e68cb326dcd55a860e90a7707031e"
)

func newTestServer(t *testing.T) http.Handler {
	t.Helper()
	return newServer(t, &fakeVirtualMachineDriver{virtualMachines: map[string]*fakeVM{}})
}

func newServer(t *testing.T, virtualMachineDriver vm.Driver) http.Handler {
	t.Helper()
	return newServerWithServices(t, virtualMachineDriver, newFakeRuntimeServices(), &fakeWireGuardManager{})
}

func newServerWithServices(
	t *testing.T,
	virtualMachineDriver vm.Driver,
	services *fakeRuntimeServices,
	wireGuardManager *fakeWireGuardManager,
) http.Handler {
	t.Helper()

	server, err := New(Config{AuthTokenHash: testTokenHash}, Dependencies{
		VirtualMachineDriver: virtualMachineDriver,
		SnapshotCreator:      services,
		SnapshotStore:        services,
		ImagePolicyStore:     services,
		WakeReconciler:       func() {},
		WireGuardManager:     wireGuardManager,
		Mesh:                 &fakeMesh{},
		Storage:              fakeCapacityProvider{},
		ConsoleBroker:        stubConsoleBroker{},
		SSHConnector:         stubSSHConnector{},
	})
	if err != nil {
		t.Fatal(err)
	}

	return server
}

type stubSSHConnector struct{}

func (stubSSHConnector) DialSSH(context.Context, string) (vm.SSHConn, error) {
	return nil, errors.New("ssh unavailable")
}

type stubConsoleBroker struct{}

func (stubConsoleBroker) Attach(context.Context, string, io.ReadWriter, <-chan console.Winsize) error {
	return console.ErrConsoleNotFound
}

const (
	validCreateRequest = `{"vcpus":1,"memory_mib":512,"disk_mib":1024,"image":{"ref":"ubuntu","architecture":"amd64","rootfs":{"url":"https://atlas.example/ubuntu.ext4?signature=secret","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"kernel":{"url":"https://atlas.example/vmlinux?signature=secret","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},"network":{"wireguard_mesh_ipv6":"fdaa:1:0:7::1","egress":"uplink"}}`
	validSSHKey        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA user@example"
)

func TestReplaceSSHKeysReturnsUpdatedVirtualMachine(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"ssh_keys":["` + validSSHKey + `"]}`
	recorder := do(t, server, http.MethodPut, "/vms/vm1/ssh-keys", body, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.SSHKeys) != 1 || response.SSHKeys[0] != validSSHKey {
		t.Fatalf("ssh keys = %v", response.SSHKeys)
	}

	recorder = do(t, server, http.MethodGet, "/vms/vm1", "", http.StatusOK)
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.SSHKeys) != 1 || response.SSHKeys[0] != validSSHKey {
		t.Fatalf("info ssh keys = %v", response.SSHKeys)
	}
}

func TestReplaceSSHKeysRejectsMissingAndDuplicateLists(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/vms/vm1/ssh-keys", `{}`, http.StatusBadRequest)
	body := `{"ssh_keys":["` + validSSHKey + `","` + validSSHKey + `"]}`
	do(t, server, http.MethodPut, "/vms/vm1/ssh-keys", body, http.StatusBadRequest)
	do(t, server, http.MethodPut, "/vms/vm1/ssh-keys", `{"ssh_keys":[]}`, http.StatusOK)
}

func TestReplaceMetadataReturnsUpdatedVirtualMachine(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"metadata":{"env":"prod","team":"platform"}}`
	recorder := do(t, server, http.MethodPut, "/vms/vm1/metadata", body, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Metadata["env"] != "prod" || response.Metadata["team"] != "platform" {
		t.Fatalf("metadata = %v", response.Metadata)
	}
}

func TestReplaceMetadataRejectsEmptyKeyAndAllowsClearing(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/vms/vm1/metadata", `{"metadata":{"":"value"}}`, http.StatusBadRequest)
	do(t, server, http.MethodPut, "/vms/vm1/metadata", `{"metadata":{}}`, http.StatusOK)
}

func TestCreateIsIdempotentAndReturnsAccepted(t *testing.T) {
	srv := newTestServer(t)
	recorder := do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	var got virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.ID != "vm1" {
		t.Errorf("id = %q, want vm1", got.ID)
	}
	if got.State != "unknown" || got.DesiredState != "running" {
		t.Errorf("state/desired = %q/%q, want unknown/running", got.State, got.DesiredState)
	}
	if got.Image.Ref != "ubuntu" || got.Image.Architecture != "amd64" {
		t.Fatalf("image = %+v", got.Image)
	}
	if got.Image.Rootfs.SHA256 != strings.Repeat("a", 64) || got.Image.Kernel.SHA256 != strings.Repeat("a", 64) {
		t.Fatalf("image artifacts = %+v", got.Image)
	}

	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if _, found := response["ip"]; found {
		t.Fatal("response contains internal guest IP")
	}
	if _, found := response["pid"]; found {
		t.Fatal("response contains Firecracker PID")
	}

	getRecorder := do(t, srv, http.MethodGet, "/vms/vm1", "", http.StatusOK)
	if err := json.Unmarshal(getRecorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Network.MAC != "06:00:00:00:00:01" {
		t.Fatalf("network MAC = %q", got.Network.MAC)
	}
}

func TestCreateRejectsLongID(t *testing.T) {
	id := strings.Repeat("a", maxResourceIDLength+1)
	do(t, newTestServer(t), http.MethodPut, "/vms/"+id, validCreateRequest, http.StatusBadRequest)
}

func TestGetUnknownIs404(t *testing.T) {
	recorder := do(t, newTestServer(t), http.MethodGet, "/vms/nope", "", http.StatusNotFound)
	var response errorResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Error.Code != "not_found" {
		t.Errorf("error code = %q, want not_found", response.Error.Code)
	}
}

func TestInternalErrorDoesNotLeakDetails(t *testing.T) {
	driver := &fakeVirtualMachineDriver{
		virtualMachines: map[string]*fakeVM{},
		listError:       errors.New("download https://images.example/rootfs?signature=secret failed"),
	}
	recorder := do(t, newServer(t, driver), http.MethodGet, "/vms", "", http.StatusInternalServerError)
	if strings.Contains(recorder.Body.String(), "secret") || strings.Contains(recorder.Body.String(), "images.example") {
		t.Fatalf("response leaked internal details: %s", recorder.Body.String())
	}
	var response errorResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Error.Code != "internal_error" {
		t.Errorf("error code = %q, want internal_error", response.Error.Code)
	}
}

func TestCreateRejectsInvalidNetwork(t *testing.T) {
	srv := newTestServer(t)
	invalidAddress := strings.Replace(validCreateRequest, "fdaa:1:0:7::1", "2001:db8::1", 1)
	invalidEgress := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"egress":"server"`, 1)
	negativePrivateThroughput := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"private_network_throughput_mibps":-1,"egress":"host"`, 1)
	negativePublicThroughput := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"public_network_throughput_mibps":-1,"egress":"host"`, 1)
	do(t, srv, http.MethodPut, "/vms/vm1", invalidAddress, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/vms/vm1", invalidEgress, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/vms/vm1", negativePrivateThroughput, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/vms/vm1", negativePublicThroughput, http.StatusBadRequest)
}

func TestCreateReturnsNetworkThroughput(t *testing.T) {
	srv := newTestServer(t)
	body := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"private_network_throughput_mibps":100,"public_network_throughput_mibps":50,"egress":"uplink"`, 1)
	recorder := do(t, srv, http.MethodPut, "/vms/vm1", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Network.PrivateNetworkThroughputMiBps != 100 || response.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("network throughput = %+v", response.Network)
	}
}

// A public IPv4 address needs an internet path. The request is rejected instead
// of silently changing the egress mode that the caller asked for.
func TestCreateRejectsPublicIPv4WithoutUplink(t *testing.T) {
	srv := newTestServer(t)
	for _, egress := range []string{"mesh", "none"} {
		body := strings.Replace(validCreateRequest, `"egress":"uplink"`,
			`"public_ipv4":"203.0.113.10","egress":"`+egress+`"`, 1)
		do(t, srv, http.MethodPut, "/vms/vm1", body, http.StatusBadRequest)
	}
}

// A mode without an internet path keeps a public limit but does not apply it.
// This lets a caller change the mode without clearing the stored limits first.
func TestCreateKeepsThePublicThroughputWithoutUplink(t *testing.T) {
	srv := newTestServer(t)
	body := strings.Replace(validCreateRequest, `"egress":"uplink"`,
		`"public_network_throughput_mibps":50,"egress":"mesh"`, 1)
	recorder := do(t, srv, http.MethodPut, "/vms/vm1", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("public throughput = %+v", response.Network)
	}
}

func TestUpdateNetworkAppliesLiveSettings(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"egress":"uplink","public_ipv4":"203.0.113.10","private_network_throughput_mibps":100,"public_network_throughput_mibps":50}`
	recorder := do(t, srv, http.MethodPut, "/vms/vm1/network", body, http.StatusOK)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Network.Egress != string(vm.EgressUplink) || response.Network.PublicIPv4 != "203.0.113.10" {
		t.Fatalf("network = %+v", response.Network)
	}
	if response.Network.PrivateNetworkThroughputMiBps != 100 || response.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("network throughput = %+v", response.Network)
	}
}

func TestUpdateNetworkAcceptsMeshAndRejectsPublicIPv4(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	recorder := do(t, srv, http.MethodPut, "/vms/vm1/network",
		`{"egress":"mesh","private_network_throughput_mibps":100}`, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Network.Egress != string(vm.EgressMesh) {
		t.Fatalf("egress = %q, want %q", response.Network.Egress, vm.EgressMesh)
	}

	do(t, srv, http.MethodPut, "/vms/vm1/network",
		`{"egress":"mesh","public_ipv4":"203.0.113.10"}`, http.StatusBadRequest)

	// Atlas resends every mutable setting, so a stored public limit must not
	// block a change to a mode that cannot apply it.
	do(t, srv, http.MethodPut, "/vms/vm1/network",
		`{"egress":"none","public_network_throughput_mibps":50}`, http.StatusOK)
}

func TestUpdateDiskLimitsAppliesAndRejects(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	recorder := do(t, srv, http.MethodPut, "/vms/vm1/disk", `{"throughput_mibps":50,"iops":2000}`, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Disk.ThroughputMiBps != 50 || response.Disk.IOPS != 2000 {
		t.Fatalf("disk = %+v", response.Disk)
	}

	do(t, srv, http.MethodPut, "/vms/vm1/disk", `{"throughput_mibps":-1}`, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/vms/vm1/disk", `{"iops":-1}`, http.StatusBadRequest)
}

func TestResizeDiskGrows(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	rec := do(t, srv, http.MethodPost, "/vms/vm1/resize/disk", `{"disk_mib":2048}`, http.StatusAccepted)
	var got virtualMachineResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Disk.SizeMiB != 2048 {
		t.Fatalf("disk size = %d, want 2048", got.Disk.SizeMiB)
	}
}

func TestResizeShrinkIs409(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPost, "/vms/vm1/resize/disk", `{"disk_mib":512}`, http.StatusConflict)
}

func TestResizeComputeNeedsStoppedVM(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPost, "/vms/vm1/resize/compute", `{"vcpus":1,"memory_mib":256}`, http.StatusConflict)
}

func TestResizeComputeRequiresBothValues(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPost, "/vms/vm1/resize/compute", `{"vcpus":1}`, http.StatusBadRequest)
}

func TestResizeComputeChecksOnlyAdditionalCapacity(t *testing.T) {
	if needsMoreThanAvailable(1024, 512, 512) {
		t.Fatal("exact memory growth capacity was rejected")
	}
	if !needsMoreThanAvailable(1025, 512, 512) {
		t.Fatal("memory growth above capacity was accepted")
	}
	if needsMoreThanAvailable(256, 512, 0) {
		t.Fatal("resource reduction required free capacity")
	}
}

func TestResizeComputeUpdatesStoppedVM(t *testing.T) {
	driver := &fakeVirtualMachineDriver{virtualMachines: map[string]*fakeVM{}}
	srv := newServer(t, driver)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)
	driver.virtualMachines["vm1"].info.State = vm.StateStopped

	do(t, srv, http.MethodPost, "/vms/vm1/resize/compute", `{"vcpus":1,"memory_mib":256}`, http.StatusAccepted)

	m := driver.virtualMachines["vm1"]
	if m.info.MemoryMiB != 256 {
		t.Errorf("memory = %d, want 256", m.info.MemoryMiB)
	}
	if m.info.DesiredState != vm.StateRunning {
		t.Errorf("desired = %q, want running", m.info.DesiredState)
	}
}

func TestHealth(t *testing.T) {
	do(t, newTestServer(t), http.MethodGet, "/health", "", http.StatusOK)
}

func TestNewRequiresAuthenticationHash(t *testing.T) {
	_, err := New(Config{}, Dependencies{})
	if err == nil {
		t.Fatal("New accepted missing authentication configuration")
	}
}

func TestSyncAppliesControllerStateAndReturnsCapacity(t *testing.T) {
	wireGuardManager := &fakeWireGuardManager{}
	services := newFakeRuntimeServices()
	driver := &fakeVirtualMachineDriver{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, wireGuardManager)

	request := `{
		"wireguard_peers":[{"node":"node-2","node_id":2,"public_key":"key-2","address":"192.0.2.2:51820"}],
		"images":[{
			"ref":"sha256:image",
			"architecture":"amd64",
			"rootfs":{"url":"https://atlas.example/rootfs","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
			"kernel":{"url":"https://atlas.example/kernel","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
			"cache_image":true
		}]
	}`
	recorder := do(t, server, http.MethodPost, "/sync", request, http.StatusOK)
	if len(wireGuardManager.peers) != 1 || wireGuardManager.peers[0].Node != "node-2" {
		t.Fatalf("peers = %+v", wireGuardManager.peers)
	}
	if len(services.policies) != 1 || services.policies[0].Name != "sha256:image" {
		t.Fatalf("image policies = %+v", services.policies)
	}

	var response syncResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Capacity.TotalStorageMiB != 1000 || response.Capacity.AvailableStorageMiB != 750 {
		t.Fatalf("storage capacity = %d/%d", response.Capacity.TotalStorageMiB, response.Capacity.AvailableStorageMiB)
	}
}

func TestSyncRequiresControllerCollections(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPost, "/sync", `{}`, http.StatusBadRequest)
	do(t, server, http.MethodPost, "/sync", `{"wireguard_peers":[]}`, http.StatusBadRequest)
}

func TestDocsSkipAuthentication(t *testing.T) {
	srv := newTestServer(t)

	for _, path := range []string{"/docs", "/docs/swagger.json"} {
		rec := httptest.NewRecorder()
		srv.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code == http.StatusUnauthorized {
			t.Fatalf("%s must not need authentication, got %d", path, rec.Code)
		}
	}

	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/vms", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("/vms without a token = %d, want 401", rec.Code)
	}
}

// do sends one request and asserts the status code.
func do(t *testing.T, srv http.Handler, method, path, body string, want int) *httptest.ResponseRecorder {
	t.Helper()
	var r io.Reader
	if body != "" {
		r = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, r)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != want {
		t.Fatalf("%s %s = %d, want %d (body %s)", method, path, rec.Code, want, rec.Body)
	}
	return rec
}

func TestCreateAndDeleteImageStagingSnapshot(t *testing.T) {
	services := newFakeRuntimeServices()
	driver := &fakeVirtualMachineDriver{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, &fakeWireGuardManager{})

	recorder := do(t, server, http.MethodPost, "/vms/vm1/snapshots", "", http.StatusCreated)
	var response snapshotCreatedResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.ID != "01900000-0000-7000-8000-000000000001" || response.Rootfs.SizeBytes != 1024 || response.Kernel.SizeBytes != 512 {
		t.Fatalf("snapshot response = %+v", response)
	}

	do(t, server, http.MethodDelete, "/snapshots/01900000-0000-7000-8000-000000000001", "", http.StatusNoContent)
	do(t, server, http.MethodDelete, "/snapshots/01900000-0000-7000-8000-000000000001", "", http.StatusNoContent)
}

func TestPauseResumeRecordDesired(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/vms/vm1", validCreateRequest, http.StatusAccepted)

	rec := do(t, srv, http.MethodPost, "/vms/vm1/actions/pause", "", http.StatusAccepted)
	var got virtualMachineResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.DesiredState != "paused" {
		t.Errorf("desired = %q, want paused", got.DesiredState)
	}
	rec = do(t, srv, http.MethodPost, "/vms/vm1/actions/resume", "", http.StatusAccepted)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.DesiredState != "running" {
		t.Errorf("desired = %q, want running", got.DesiredState)
	}
}

func TestRemovedSnapshotAndImageRoutesReturnNotFound(t *testing.T) {
	server := newTestServer(t)
	for _, request := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/images"},
		{http.MethodGet, "/vms/vm1/snapshots"},
		{http.MethodPost, "/vms/vm1/snapshots/snapshot-1/restore"},
	} {
		expectedStatus := http.StatusNotFound
		if request.path == "/vms/vm1/snapshots" {
			expectedStatus = http.StatusMethodNotAllowed
		}
		do(t, server, request.method, request.path, "", expectedStatus)
	}
}
