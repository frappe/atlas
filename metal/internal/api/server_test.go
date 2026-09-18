package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeVM struct {
	info vm.Information
}

type fakeVirtualMachineManager struct {
	virtualMachines map[string]*fakeVM
	listError       error
	services        *fakeRuntimeServices
	deferMetadata   bool
}

func (manager *fakeVirtualMachineManager) Create(_ context.Context, id string, specification vm.Specification) (vm.Information, error) {
	if existing, found := manager.virtualMachines[id]; found {
		return existing.info, nil
	}

	virtualMachine := &fakeVM{info: vm.Information{
		ID:                            id,
		State:                         vm.StateUnknown,
		DesiredState:                  vm.StateRunning,
		CPUMillicores:                 specification.CPUMillicores,
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
		Firewall:                      specification.Network.Firewall,
		SleepAfterIdleSeconds:         specification.SleepAfterIdleSeconds,
		DesiredGeneration:             1,
	}}
	manager.virtualMachines[id] = virtualMachine

	return virtualMachine.info, nil
}

func (manager *fakeVirtualMachineManager) Information(_ context.Context, id string) (vm.Information, error) {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.Information{}, vm.ErrNotFound
	}

	return virtualMachine.info, nil
}

func (manager *fakeVirtualMachineManager) List(context.Context) ([]vm.Information, error) {
	if manager.listError != nil {
		return nil, manager.listError
	}

	virtualMachines := make([]vm.Information, 0, len(manager.virtualMachines))
	for _, virtualMachine := range manager.virtualMachines {
		virtualMachines = append(virtualMachines, virtualMachine.info)
	}

	return virtualMachines, nil
}

func (manager *fakeVirtualMachineManager) SetPowerState(_ context.Context, id string, state vm.State) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}

	virtualMachine.info.DesiredState = state
	virtualMachine.info.DesiredGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) ReplaceSSHKeys(
	_ context.Context,
	id string,
	sshKeys []string,
) (bool, error) {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return false, vm.ErrNotFound
	}
	virtualMachine.info.SSHKeys = append([]string(nil), sshKeys...)
	virtualMachine.info.DesiredGeneration++
	return !manager.deferMetadata, nil
}

func (manager *fakeVirtualMachineManager) ReplaceMetadata(
	_ context.Context,
	id string,
	metadata map[string]string,
) (bool, error) {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return false, vm.ErrNotFound
	}
	virtualMachine.info.Metadata = maps.Clone(metadata)
	virtualMachine.info.DesiredGeneration++
	return !manager.deferMetadata, nil
}

func (manager *fakeVirtualMachineManager) SetNetwork(_ context.Context, id string, configuration vm.NetworkConfiguration) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.Egress = configuration.Egress
	virtualMachine.info.PublicIPv4 = configuration.PublicIPv4
	virtualMachine.info.WireGuardMeshIPv6 = configuration.WireGuardMeshIPv6
	virtualMachine.info.PrivateNetworkThroughputMiBps = configuration.PrivateNetworkThroughputMiBps
	virtualMachine.info.PublicNetworkThroughputMiBps = configuration.PublicNetworkThroughputMiBps
	virtualMachine.info.Firewall = configuration.Firewall
	virtualMachine.info.DesiredGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) SetDisk(_ context.Context, id string, diskMiB int, limits vm.Disk) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.DiskThroughputMiBps = limits.ThroughputMiBps
	virtualMachine.info.DiskIOPS = limits.IOPS
	if diskMiB < virtualMachine.info.DiskMiB {
		return vm.ErrConflict
	}
	virtualMachine.info.DiskMiB = diskMiB
	virtualMachine.info.DesiredGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) SetCompute(_ context.Context, id string, compute vm.Compute) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	// A shape change needs a stopped VM. An idle timeout change does not.
	shapeChanged := virtualMachine.info.CPUMillicores != compute.CPUMillicores ||
		virtualMachine.info.MemoryMiB != compute.MemoryMiB
	if shapeChanged {
		if virtualMachine.info.State != vm.StateStopped {
			return vm.ErrConflict
		}
		virtualMachine.info.CPUMillicores = compute.CPUMillicores
		virtualMachine.info.MemoryMiB = compute.MemoryMiB
		virtualMachine.info.DesiredState = vm.StateRunning
	}

	virtualMachine.info.SleepAfterIdleSeconds = compute.SleepAfterIdleSeconds
	virtualMachine.info.DesiredGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) RequestRestart(_ context.Context, id string) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	if virtualMachine.info.DesiredState != vm.StateRunning {
		return vm.ErrConflict
	}
	virtualMachine.info.State = vm.StateRunning
	virtualMachine.info.DesiredRestartGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) Delete(_ context.Context, id string) error {
	virtualMachine, found := manager.virtualMachines[id]
	if !found {
		return vm.ErrNotFound
	}
	virtualMachine.info.DesiredState = vm.StateDestroyed
	virtualMachine.info.DesiredGeneration++
	return nil
}

func (manager *fakeVirtualMachineManager) CreateSnapshot(_ context.Context, id string) (vm.StagedSnapshot, error) {
	snapshotID := "01900000-0000-7000-8000-000000000001"
	manager.services.snapshots[snapshotID] = storage.StagedSnapshot{
		ID: snapshotID, SourceVirtualMachineID: id,
		Rootfs: storage.ArtifactSize{SizeBytes: 1024}, Kernel: storage.ArtifactSize{SizeBytes: 512},
	}
	return vm.StagedSnapshot{ID: snapshotID, SourceVirtualMachineID: id, RootfsSizeBytes: 1024, KernelSizeBytes: 512}, nil
}

func (manager *fakeVirtualMachineManager) ConnectSSH(context.Context, string) (vm.SSHConnection, error) {
	return nil, errors.New("ssh unavailable")
}

type fakeRuntimeServices struct {
	policies        []vm.Image
	privileged      []string
	unicastPeers    []netip.Addr
	unicastDisabled bool
	snapshots       map[string]storage.StagedSnapshot
}

func (services *fakeRuntimeServices) ApplyPrivilegedAddresses(
	_ context.Context,
	addresses []string,
) error {
	services.privileged = append([]string(nil), addresses...)
	return nil
}

func (services *fakeRuntimeServices) Apply(_ context.Context, peers []netip.Addr) error {
	services.unicastPeers = append([]netip.Addr(nil), peers...)
	return nil
}

func (services *fakeRuntimeServices) Disable(context.Context) error {
	services.unicastDisabled = true
	return nil
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

func (services *fakeRuntimeServices) SetImagePolicies(_ context.Context, images []vm.Image) error {
	services.policies = append([]vm.Image(nil), images...)
	return nil
}

type fakeWireGuardManager struct {
	peers []network.WireGuardPeer
}

func (manager *fakeWireGuardManager) Apply(_ context.Context, peers []network.WireGuardPeer) error {
	manager.peers = append([]network.WireGuardPeer(nil), peers...)
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
	return newServer(t, &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}})
}

func newServer(t *testing.T, virtualMachineManager VirtualMachineManager) http.Handler {
	t.Helper()
	return newServerWithServices(t, virtualMachineManager, newFakeRuntimeServices(), &fakeWireGuardManager{})
}

func newServerWithServices(
	t *testing.T,
	virtualMachineManager VirtualMachineManager,
	services *fakeRuntimeServices,
	wireGuardManager *fakeWireGuardManager,
) http.Handler {
	t.Helper()

	if manager, ok := virtualMachineManager.(*fakeVirtualMachineManager); ok {
		manager.services = services
	}
	hostService, err := host.NewService(host.Dependencies{
		Mesh: services, WireGuard: wireGuardManager, Images: services,
		Unicast:         services,
		VirtualMachines: virtualMachineManager, Storage: fakeCapacityProvider{}, Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}
	server, err := New(Config{AuthTokenHash: testTokenHash}, Dependencies{
		VirtualMachineManager: virtualMachineManager,
		MigrationManager:      &stubMigrationManager{},
		SnapshotStore:         services,
		WakeReconciler:        func() {},
		HostService:           hostService,
		SerialBroker:          stubSerialBroker{},
	})
	if err != nil {
		t.Fatal(err)
	}

	return server
}

func TestCorrelationHeadersAreGeneratedAndPreserveSafeValues(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/health", nil)
	request.Header.Set("X-Request-ID", "request-123")
	request.Header.Set("X-Operation-ID", "operation-456")
	recorder := httptest.NewRecorder()

	server.ServeHTTP(recorder, request)

	if got := recorder.Header().Get("X-Request-ID"); got != "request-123" {
		t.Fatalf("request ID: got %q", got)
	}
	if got := recorder.Header().Get("X-Operation-ID"); got != "operation-456" {
		t.Fatalf("operation ID: got %q", got)
	}
}

type stubSerialBroker struct{}

func (stubSerialBroker) Attach(context.Context, string, io.ReadWriter, <-chan console.Winsize) error {
	return console.ErrConsoleNotFound
}

const (
	validCreateRequest = `{"compute":{"cpu_millicores":1000,"memory_mib":512},"disk":{"size_mib":1024,"throughput_mibps":0,"iops":0},"image":{"ref":"ubuntu","architecture":"amd64","rootfs":{"url":"https://atlas.example/ubuntu.ext4?signature=secret","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"kernel":{"url":"https://atlas.example/vmlinux?signature=secret","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},"network":{"wireguard_mesh_ipv6":"fdaa:1:0:7::1","egress":"uplink","firewall":{"enabled":false,"inbound":[],"outbound":[]}},"guest":{"hostname":"vm1","ssh_keys":[],"metadata":{},"user_data":""}}`
	validSSHKey        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA user@example"
)

func TestReplaceSSHKeysReturnsUpdatedVirtualMachine(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"ssh_keys":["` + validSSHKey + `"]}`
	recorder := do(t, server, http.MethodPut, "/v1/vms/vm1/ssh-keys", body, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.Desired.Guest.SSHKeys) != 1 || response.Desired.Guest.SSHKeys[0] != validSSHKey {
		t.Fatalf("ssh keys = %v", response.Desired.Guest.SSHKeys)
	}

	recorder = do(t, server, http.MethodGet, "/v1/vms/vm1", "", http.StatusOK)
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.Desired.Guest.SSHKeys) != 1 || response.Desired.Guest.SSHKeys[0] != validSSHKey {
		t.Fatalf("info ssh keys = %v", response.Desired.Guest.SSHKeys)
	}
}

func TestReplaceSSHKeysRejectsMissingAndDuplicateLists(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/v1/vms/vm1/ssh-keys", `{}`, http.StatusBadRequest)
	body := `{"ssh_keys":["` + validSSHKey + `","` + validSSHKey + `"]}`
	do(t, server, http.MethodPut, "/v1/vms/vm1/ssh-keys", body, http.StatusBadRequest)
	do(t, server, http.MethodPut, "/v1/vms/vm1/ssh-keys", `{"ssh_keys":[]}`, http.StatusOK)
}

func TestReplaceMetadataReturnsUpdatedVirtualMachine(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"metadata":{"env":"prod","team":"platform"}}`
	recorder := do(t, server, http.MethodPut, "/v1/vms/vm1/metadata", body, http.StatusOK)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Guest.Metadata["env"] != "prod" || response.Desired.Guest.Metadata["team"] != "platform" {
		t.Fatalf("metadata = %v", response.Desired.Guest.Metadata)
	}
}

func TestReplaceMetadataRejectsEmptyKeyAndAllowsClearing(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/v1/vms/vm1/metadata", `{"metadata":{"":"value"}}`, http.StatusBadRequest)
	do(t, server, http.MethodPut, "/v1/vms/vm1/metadata", `{"metadata":{}}`, http.StatusOK)
}

func TestImmediateMetadataUpdateReturnsAcceptedWhenReconciliationMustContinue(t *testing.T) {
	manager := &fakeVirtualMachineManager{
		virtualMachines: map[string]*fakeVM{},
		deferMetadata:   true,
	}
	server := newServer(t, manager)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/v1/vms/vm1/metadata", `{"metadata":{"env":"prod"}}`, http.StatusAccepted)
	do(t, server, http.MethodPut, "/v1/vms/vm1/ssh-keys", `{"ssh_keys":[]}`, http.StatusAccepted)
}

func TestCreateIsIdempotentAndReturnsAccepted(t *testing.T) {
	srv := newTestServer(t)
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	var got virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.ID != "vm1" {
		t.Errorf("id = %q, want vm1", got.ID)
	}
	if got.Observed.State != "unknown" || got.Desired.State != "running" {
		t.Errorf("state/desired = %q/%q, want unknown/running", got.Observed.State, got.Desired.State)
	}
	if got.Desired.Image.Ref != "ubuntu" || got.Desired.Image.Architecture != "amd64" {
		t.Fatalf("image = %+v", got.Desired.Image)
	}
	if got.Desired.Image.Rootfs.SHA256 != strings.Repeat("a", 64) || got.Desired.Image.Kernel.SHA256 != strings.Repeat("a", 64) {
		t.Fatalf("image artifacts = %+v", got.Desired.Image)
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
	for _, field := range []string{"state", "desired_state", "vcpus", "memory_mib", "disk", "network", "user_data"} {
		if _, found := response[field]; found {
			t.Fatalf("response contains old or private top-level field %q", field)
		}
	}
	if strings.Contains(recorder.Body.String(), "signature=secret") {
		t.Fatal("response contains a signed image URL")
	}

	getRecorder := do(t, srv, http.MethodGet, "/v1/vms/vm1", "", http.StatusOK)
	if err := json.Unmarshal(getRecorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Observed.Network.MAC != "06:00:00:00:00:01" {
		t.Fatalf("network MAC = %q", got.Observed.Network.MAC)
	}
}

func TestListReturnsAnArrayOfNestedResources(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	recorder := do(t, server, http.MethodGet, "/v1/vms", "", http.StatusOK)

	var response []virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response) != 1 || response[0].Desired.Compute.CPUMillicores != 1000 {
		t.Fatalf("virtual machine list = %+v", response)
	}
}

func TestMutationRequestsRejectUnknownAndTrailingJSON(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/v1/vms/vm1/power", `{"state":"stopped","unknown":true}`, http.StatusBadRequest)
	do(t, server, http.MethodPut, "/v1/vms/vm1/power", `{"state":"stopped"}{}`, http.StatusBadRequest)
}

func TestPowerRequestRejectsWarm(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	do(t, server, http.MethodPut, "/v1/vms/vm1/power", `{"state":"stopped","warm":true}`, http.StatusBadRequest)
}

func TestCreateRejectsLongID(t *testing.T) {
	id := strings.Repeat("a", maximumResourceIDLength+1)
	do(t, newTestServer(t), http.MethodPut, "/v1/vms/"+id, validCreateRequest, http.StatusBadRequest)
}

func TestGetUnknownIs404(t *testing.T) {
	recorder := do(t, newTestServer(t), http.MethodGet, "/v1/vms/nope", "", http.StatusNotFound)
	var response errorResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Error.Code != "not_found" {
		t.Errorf("error code = %q, want not_found", response.Error.Code)
	}
}

func TestInternalErrorDoesNotLeakDetails(t *testing.T) {
	driver := &fakeVirtualMachineManager{
		virtualMachines: map[string]*fakeVM{},
		listError:       errors.New("download https://images.example/rootfs?signature=secret failed"),
	}
	recorder := do(t, newServer(t, driver), http.MethodGet, "/v1/vms", "", http.StatusInternalServerError)
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
	if !response.Error.Retryable {
		t.Fatal("internal error must be retryable")
	}
}

func TestCreateRejectsInvalidNetwork(t *testing.T) {
	srv := newTestServer(t)
	invalidAddress := strings.Replace(validCreateRequest, "fdaa:1:0:7::1", "2001:db8::1", 1)
	invalidEgress := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"egress":"server"`, 1)
	negativePrivateThroughput := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"private_network_throughput_mibps":-1,"egress":"host"`, 1)
	negativePublicThroughput := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"public_network_throughput_mibps":-1,"egress":"host"`, 1)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", invalidAddress, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", invalidEgress, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", negativePrivateThroughput, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", negativePublicThroughput, http.StatusBadRequest)
}

func TestCreateReturnsNetworkThroughput(t *testing.T) {
	srv := newTestServer(t)
	body := strings.Replace(validCreateRequest, `"egress":"uplink"`, `"private_network_throughput_mibps":100,"public_network_throughput_mibps":50,"egress":"uplink"`, 1)
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Network.PrivateNetworkThroughputMiBps != 100 || response.Desired.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("network throughput = %+v", response.Desired.Network)
	}
}

// A public IPv4 address requires internet egress.
func TestCreateRejectsPublicIPv4WithoutUplink(t *testing.T) {
	srv := newTestServer(t)
	for _, egress := range []string{"mesh", "none"} {
		body := strings.Replace(validCreateRequest, `"egress":"uplink"`,
			`"public_ipv4":"203.0.113.10","egress":"`+egress+`"`, 1)
		do(t, srv, http.MethodPut, "/v1/vms/vm1", body, http.StatusBadRequest)
	}
}

// A mode without internet egress keeps, but does not apply, public limits.
func TestCreateKeepsThePublicThroughputWithoutUplink(t *testing.T) {
	srv := newTestServer(t)
	body := strings.Replace(validCreateRequest, `"egress":"uplink"`,
		`"public_network_throughput_mibps":50,"egress":"mesh"`, 1)
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("public throughput = %+v", response.Desired.Network)
	}
}

func TestCreateStoresAndReturnsTheIdleTimeout(t *testing.T) {
	srv := newTestServer(t)
	body := strings.Replace(validCreateRequest,
		`"compute":{"cpu_millicores":1000,"memory_mib":512}`,
		`"compute":{"cpu_millicores":1000,"memory_mib":512,"sleep_after_idle_seconds":1800}`, 1)
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Compute.SleepAfterIdleSeconds != 1800 {
		t.Fatalf("sleep_after_idle_seconds = %d, want 1800", response.Desired.Compute.SleepAfterIdleSeconds)
	}
}

func TestCreateDefaultsToNoIdleShutdown(t *testing.T) {
	srv := newTestServer(t)
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Compute.SleepAfterIdleSeconds != 0 {
		t.Fatalf("sleep_after_idle_seconds = %d, want 0", response.Desired.Compute.SleepAfterIdleSeconds)
	}
}

func TestPowerRequestRejectsUnknownState(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/power", `{"state":"sleeping"}`, http.StatusBadRequest)
}

func TestSetComputeStoresTheIdleTimeout(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"cpu_millicores":1000,"memory_mib":512,"sleep_after_idle_seconds":1800}`
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", body, http.StatusAccepted)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Compute.SleepAfterIdleSeconds != 1800 {
		t.Errorf("compute.sleep_after_idle_seconds = %d, want 1800", response.Desired.Compute.SleepAfterIdleSeconds)
	}
}

func TestSetComputeRejectsANegativeIdleTimeout(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"cpu_millicores":1000,"memory_mib":512,"sleep_after_idle_seconds":-1}`
	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", body, http.StatusBadRequest)
}

func TestSetComputeRejectsAnUnsafeIdleTimeout(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"cpu_millicores":1000,"memory_mib":512,"sleep_after_idle_seconds":9223372037}`
	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", body, http.StatusBadRequest)
}

func TestSetNetworkStoresTheCompleteSpecification(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	body := `{"egress":"uplink","public_ipv4":"203.0.113.10","wireguard_mesh_ipv6":"fdaa:1:0:7::1","private_network_throughput_mibps":100,"public_network_throughput_mibps":50,"firewall":{"enabled":true,"inbound":[{"protocol":"tcp","ports":"22","cidrs":["203.0.113.0/24"]}],"outbound":[]}}`
	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1/network", body, http.StatusAccepted)

	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Network.Egress != string(vm.EgressUplink) || response.Desired.Network.PublicIPv4 != "203.0.113.10" {
		t.Fatalf("network = %+v", response.Desired.Network)
	}
	if response.Desired.Network.PrivateNetworkThroughputMiBps != 100 || response.Desired.Network.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("network throughput = %+v", response.Desired.Network)
	}
	if !response.Desired.Network.Firewall.Enabled || len(response.Desired.Network.Firewall.Inbound) != 1 {
		t.Fatalf("firewall = %+v", response.Desired.Network.Firewall)
	}
}

func TestSetNetworkAcceptsMeshAndRejectsPublicIPv4(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1/network",
		`{"egress":"mesh","wireguard_mesh_ipv6":"fdaa:1:0:7::1","private_network_throughput_mibps":100,"firewall":{"enabled":false,"inbound":[],"outbound":[]}}`, http.StatusAccepted)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Network.Egress != string(vm.EgressMesh) {
		t.Fatalf("egress = %q, want %q", response.Desired.Network.Egress, vm.EgressMesh)
	}

	do(t, srv, http.MethodPut, "/v1/vms/vm1/network",
		`{"egress":"mesh","public_ipv4":"203.0.113.10","wireguard_mesh_ipv6":"fdaa:1:0:7::1","firewall":{"enabled":false,"inbound":[],"outbound":[]}}`, http.StatusBadRequest)

	// Stored public limits must not block an egress mode change.
	do(t, srv, http.MethodPut, "/v1/vms/vm1/network",
		`{"egress":"none","wireguard_mesh_ipv6":"fdaa:1:0:7::1","public_network_throughput_mibps":50,"firewall":{"enabled":false,"inbound":[],"outbound":[]}}`, http.StatusAccepted)
}

func TestSetDiskAppliesCompleteSpecificationAndRejectsInvalidLimits(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	recorder := do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":1024,"throughput_mibps":50,"iops":2000}`, http.StatusAccepted)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.Disk.ThroughputMiBps != 50 || response.Desired.Disk.IOPS != 2000 {
		t.Fatalf("disk = %+v", response.Desired.Disk)
	}

	do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":1024,"throughput_mibps":-1}`, http.StatusBadRequest)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":1024,"iops":-1}`, http.StatusBadRequest)
}

func TestSetDiskGrows(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	rec := do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":1500,"throughput_mibps":0,"iops":0}`, http.StatusAccepted)
	var got virtualMachineResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Desired.Disk.SizeMiB != 1500 {
		t.Fatalf("disk size = %d, want 1500", got.Desired.Disk.SizeMiB)
	}
}

func TestSetDiskRejectsInsufficientCapacity(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":2048,"throughput_mibps":0,"iops":0}`, http.StatusConflict)
}

func TestSetDiskRejectsShrink(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/disk", `{"size_mib":512,"throughput_mibps":0,"iops":0}`, http.StatusConflict)
}

func TestSetComputeNeedsStoppedVM(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", `{"cpu_millicores":500,"memory_mib":512}`, http.StatusConflict)
}

func TestSetComputeRequiresBothValues(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", `{"cpu_millicores":500}`, http.StatusBadRequest)
}

func TestSetComputeChecksOnlyAdditionalCapacity(t *testing.T) {
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

func TestPublicAPIErrorMapsUploadAndShutdownErrors(t *testing.T) {
	for _, testCase := range []struct {
		name   string
		err    error
		status int
		code   string
	}{
		{name: "invalid upload", err: storage.ErrInvalidUpload, status: http.StatusBadRequest, code: "invalid_request"},
		{name: "shutdown", err: storage.ErrShuttingDown, status: http.StatusServiceUnavailable, code: "unavailable"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			apiError := publicAPIError(testCase.err)
			if apiError.status != testCase.status || apiError.code != testCase.code {
				t.Fatalf("api error = %d/%q, want %d/%q", apiError.status, apiError.code, testCase.status, testCase.code)
			}
		})
	}
}

func TestSetComputeUpdatesStoppedVM(t *testing.T) {
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	srv := newServer(t, driver)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	driver.virtualMachines["vm1"].info.State = vm.StateStopped

	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", `{"cpu_millicores":500,"memory_mib":256}`, http.StatusAccepted)

	m := driver.virtualMachines["vm1"]
	if m.info.MemoryMiB != 256 {
		t.Errorf("memory = %d, want 256", m.info.MemoryMiB)
	}
	if m.info.DesiredState != vm.StateRunning {
		t.Errorf("desired = %q, want running", m.info.DesiredState)
	}
}

func TestSetComputeAcceptsMaximumCPUEntitlement(t *testing.T) {
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	srv := newServer(t, driver)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)
	driver.virtualMachines["vm1"].info.State = vm.StateStopped

	do(t, srv, http.MethodPut, "/v1/vms/vm1/compute", `{"cpu_millicores":32000,"memory_mib":256}`, http.StatusAccepted)

	if got := driver.virtualMachines["vm1"].info.CPUMillicores; got != 32000 {
		t.Errorf("CPU millicores = %d, want 32000", got)
	}
}

func TestComputeRejectsCPUEntitlementOutsideFirecrackerRange(t *testing.T) {
	srv := newTestServer(t)

	for _, cpuMillicores := range []int{99, 32001} {
		body := strings.Replace(validCreateRequest, `"cpu_millicores":1000`,
			fmt.Sprintf(`"cpu_millicores":%d`, cpuMillicores), 1)
		do(t, srv, http.MethodPut, "/v1/vms/vm1", body, http.StatusBadRequest)
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
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, wireGuardManager)

	request := `{
		"wireguard_peers":[{"node":"node-2","node_id":2,"public_key":"key-2","address":"192.0.2.2:51820"}],
		"images":[{
			"ref":"sha256:image",
			"architecture":"amd64",
			"rootfs":{"url":"https://atlas.example/rootfs","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
			"kernel":{"url":"https://atlas.example/kernel","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
			"cache_image":true
		}],
		"privileged_vm_addresses":["fdaa:1:0:0::1"]
	}`
	recorder := do(t, server, http.MethodPost, "/v1/sync", request, http.StatusOK)
	if len(services.privileged) != 1 || services.privileged[0] != "fdaa:1:0:0::1" {
		t.Fatalf("privileged addresses = %+v", services.privileged)
	}
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

// A host not-found has no addressed resource. A 404 tells the controller to
// stop, so the sync must report a retryable host fault instead.
func TestSyncReportsHostNotFoundAsUnavailable(t *testing.T) {
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}, listError: vm.ErrNotFound}
	server := newServerWithServices(t, driver, newFakeRuntimeServices(), &fakeWireGuardManager{})

	recorder := do(
		t, server, http.MethodPost, "/v1/sync",
		`{"wireguard_peers":[],"images":[],"privileged_vm_addresses":[]}`,
		http.StatusServiceUnavailable,
	)

	var response errorResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if !response.Error.Retryable || response.Error.Code != "unavailable" {
		t.Fatalf("error = %+v", response.Error)
	}
}

// A missing collection would read as an empty set and clear host state.
func TestSyncRequiresControllerCollections(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPost, "/v1/sync", `{}`, http.StatusBadRequest)
	do(t, server, http.MethodPost, "/v1/sync", `{"wireguard_peers":[]}`, http.StatusBadRequest)
	do(t, server, http.MethodPost, "/v1/sync", `{"wireguard_peers":[],"images":[]}`, http.StatusBadRequest)
	do(
		t, server, http.MethodPost, "/v1/sync",
		`{"wireguard_peers":[],"images":[],"privileged_vm_addresses":[]}`,
		http.StatusOK,
	)
}

func TestSyncAppliesTheUnicastPeerSetInUnicastMode(t *testing.T) {
	services := newFakeRuntimeServices()
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, &fakeWireGuardManager{})

	request := `{
		"wireguard_peers":[],
		"images":[],
		"privileged_vm_addresses":[],
		"unicast_peers":["10.20.0.11","10.20.0.12"]
	}`
	do(t, server, http.MethodPost, "/v1/sync", request, http.StatusOK)

	if len(services.unicastPeers) != 2 || services.unicastPeers[0] != netip.MustParseAddr("10.20.0.11") {
		t.Fatalf("unicast peers = %v", services.unicastPeers)
	}
	if services.unicastDisabled {
		t.Fatal("unicast transport was disabled in unicast mode")
	}
}

func TestSyncDisablesTheUnicastTransportInMulticastMode(t *testing.T) {
	services := newFakeRuntimeServices()
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, &fakeWireGuardManager{})

	do(
		t, server, http.MethodPost, "/v1/sync",
		`{"wireguard_peers":[],"images":[],"privileged_vm_addresses":[]}`,
		http.StatusOK,
	)

	if !services.unicastDisabled || services.unicastPeers != nil {
		t.Fatalf("unicast transport = peers %v disabled %t, want a disabled transport", services.unicastPeers, services.unicastDisabled)
	}
}

func TestSyncRejectsAnInvalidUnicastPeerSet(t *testing.T) {
	server := newTestServer(t)

	cases := map[string]string{
		"not an IPv4 address": `{"wireguard_peers":[],"images":[],"privileged_vm_addresses":[],"unicast_peers":["fdab::1"]}`,
		"repeated peer":       `{"wireguard_peers":[],"images":[],"privileged_vm_addresses":[],"unicast_peers":["10.20.0.11","10.20.0.11"]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			do(t, server, http.MethodPost, "/v1/sync", body, http.StatusBadRequest)
		})
	}
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
	srv.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/vms", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("/v1/vms without a token = %d, want 401", rec.Code)
	}
}

func TestOpenAPIDocumentContainsOnlyTheVersionedControllerContract(t *testing.T) {
	server := newTestServer(t)
	recorder := httptest.NewRecorder()
	server.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/docs/swagger.json", nil))

	var document struct {
		Paths map[string]map[string]struct {
			Security []map[string][]string `json:"security"`
		} `json:"paths"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}
	if _, found := document.Paths["/v1/vms/{id}/power"]; !found {
		t.Fatal("OpenAPI document does not contain the versioned power route")
	}
	for _, path := range []string{"/vms", "/sync", "/snapshots/{id}"} {
		if _, found := document.Paths[path]; found {
			t.Fatalf("OpenAPI document contains unversioned route %q", path)
		}
	}
	if len(document.Paths["/health"]["get"].Security) != 0 {
		t.Fatal("OpenAPI health route requires authentication")
	}
	if len(document.Paths["/v1/vms"]["get"].Security) == 0 {
		t.Fatal("OpenAPI virtual machine route has no authentication")
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
	driver := &fakeVirtualMachineManager{virtualMachines: map[string]*fakeVM{}}
	server := newServerWithServices(t, driver, services, &fakeWireGuardManager{})

	recorder := do(t, server, http.MethodPost, "/v1/vms/vm1/snapshots", "", http.StatusCreated)
	var response snapshotCreatedResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.ID != "01900000-0000-7000-8000-000000000001" || response.Rootfs.SizeBytes != 1024 || response.Kernel.SizeBytes != 512 {
		t.Fatalf("snapshot response = %+v", response)
	}

	do(t, server, http.MethodDelete, "/v1/snapshots/01900000-0000-7000-8000-000000000001", "", http.StatusNoContent)
	do(t, server, http.MethodDelete, "/v1/snapshots/01900000-0000-7000-8000-000000000001", "", http.StatusNoContent)
}

func TestPauseResumeRecordDesired(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	rec := do(t, srv, http.MethodPut, "/v1/vms/vm1/power", `{"state":"paused"}`, http.StatusAccepted)
	var got virtualMachineResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Desired.State != "paused" {
		t.Errorf("desired = %q, want paused", got.Desired.State)
	}
	rec = do(t, srv, http.MethodPut, "/v1/vms/vm1/power", `{"state":"running"}`, http.StatusAccepted)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Desired.State != "running" {
		t.Errorf("desired = %q, want running", got.Desired.State)
	}
}

func TestRestartAndDeleteReturnUpdatedResources(t *testing.T) {
	server := newTestServer(t)
	do(t, server, http.MethodPut, "/v1/vms/vm1", validCreateRequest, http.StatusAccepted)

	recorder := do(t, server, http.MethodPost, "/v1/vms/vm1/restart", "", http.StatusAccepted)
	var response virtualMachineResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.RestartGeneration != 1 {
		t.Fatalf("restart generation = %d, want 1", response.Desired.RestartGeneration)
	}

	recorder = do(t, server, http.MethodDelete, "/v1/vms/vm1", "", http.StatusAccepted)
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Desired.State != string(vm.StateDestroyed) {
		t.Fatalf("desired state = %q, want destroyed", response.Desired.State)
	}
}

func TestUnversionedControllerRoutesAreNotAvailable(t *testing.T) {
	server := newTestServer(t)
	for _, request := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/sync"},
		{http.MethodGet, "/vms"},
		{http.MethodPut, "/vms/vm1"},
		{http.MethodPost, "/vms/vm1/actions/start"},
		{http.MethodPost, "/vms/vm1/resize/disk"},
		{http.MethodGet, "/snapshots/snapshot-1"},
	} {
		do(t, server, request.method, request.path, "", http.StatusNotFound)
	}
}

func TestRemovedSnapshotAndImageRoutesReturnNotFound(t *testing.T) {
	server := newTestServer(t)
	for _, request := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/images"},
		{http.MethodGet, "/v1/vms/vm1/snapshots"},
		{http.MethodPost, "/v1/vms/vm1/snapshots/snapshot-1/restore"},
	} {
		do(t, server, request.method, request.path, "", http.StatusNotFound)
	}
}
