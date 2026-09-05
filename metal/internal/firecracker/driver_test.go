package firecracker

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/frappe/atlas/metal/internal/idalloc"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

type fakeNetwork struct {
	allocateCalls  int
	releaseCalls   int
	releaseError   error
	request        network.Request
	releaseRequest network.ReleaseRequest
	updateRequest  network.UpdateRequest
}

func (f *fakeNetwork) Allocate(_ context.Context, request network.Request) (network.Interface, error) {
	f.allocateCalls++
	f.request = request
	return network.Interface{GuestIPAddress: "172.16.0.2", MACAddress: "06:00:ac:10:00:02"}, nil
}

func (f *fakeNetwork) Resolve(string) network.Interface { return network.Interface{} }

func (f *fakeNetwork) Update(_ context.Context, request network.UpdateRequest) error {
	f.updateRequest = request
	return nil
}

func (f *fakeNetwork) Release(_ context.Context, request network.ReleaseRequest) error {
	f.releaseCalls++
	f.releaseRequest = request
	return f.releaseError
}

func testDriver(t *testing.T) (*Driver, *fakeNetwork, *fakeImages) {
	t.Helper()
	directory := t.TempDir()
	networkAllocator := &fakeNetwork{}
	images := &fakeImages{}
	driver := New(Config{
		MachinesDir: filepath.Join(directory, "machines"),
		SocketsDir:  filepath.Join(directory, "sockets"),
		IDs:         idalloc.Range{Min: 1000, Max: 1001},
	}, &stubUnits{}, images, images, images, networkAllocator, &stubConsoleBroker{})
	return driver, networkAllocator, images
}

type stubConsoleBroker struct{}

func (*stubConsoleBroker) Open(string) error  { return nil }
func (*stubConsoleBroker) Close(string) error { return nil }

func TestBootArgs(t *testing.T) {
	boot := storage.BootConfiguration{KernelArgs: "console=ttyS0"}
	networkInterface := network.Interface{GuestIPAddress: "172.16.0.2", GatewayIPAddress: "172.16.0.1"}
	got := bootArguments(boot, networkInterface)
	want := "console=ttyS0 ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off"
	if got != want {
		t.Errorf("bootArguments = %q", got)
	}
}

func TestCreateIsIdempotentForStableID(t *testing.T) {
	driver, networkAllocator, _ := testDriver(t)
	specification := vm.Spec{VCPUs: 1, MemoryMiB: 128, DiskMiB: 1024}

	first, err := driver.Create(context.Background(), "vm-1", specification)
	if err != nil {
		t.Fatal(err)
	}
	second, err := driver.Create(context.Background(), "vm-1", specification)
	if err != nil {
		t.Fatal(err)
	}
	if first.ID() != "vm-1" || second.ID() != "vm-1" {
		t.Fatalf("created IDs = %q and %q", first.ID(), second.ID())
	}
	if networkAllocator.allocateCalls != 0 {
		t.Fatalf("network allocations = %d, want 0", networkAllocator.allocateCalls)
	}
}

func TestCreateRejectsChangedSpecForStableID(t *testing.T) {
	driver, _, _ := testDriver(t)
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{VCPUs: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{VCPUs: 2}); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("create changed spec = %v, want ErrConflict", err)
	}
}

func TestAllocateNetworkPassesThroughputLimits(t *testing.T) {
	driver, networkAllocator, _ := testDriver(t)
	configuration := vmConfig{
		ID:  "vm-1",
		UID: 1000,
		GID: 1000,
		Spec: vm.Spec{Network: vm.Network{
			PrivateNetworkThroughputMiBps: 100,
			PublicNetworkThroughputMiBps:  50,
		}},
	}

	if _, err := driver.allocateNetwork(context.Background(), configuration); err != nil {
		t.Fatal(err)
	}
	if networkAllocator.allocateCalls != 1 {
		t.Fatalf("network allocations = %d, want 1", networkAllocator.allocateCalls)
	}
	if networkAllocator.request.PrivateNetworkThroughputMiBps != 100 || networkAllocator.request.PublicNetworkThroughputMiBps != 50 {
		t.Fatalf("throughput limits = %+v", networkAllocator.request)
	}
}

func TestRestoreNetworksReplaysExistingVirtualMachineNetworks(t *testing.T) {
	driver, networkAllocator, _ := testDriver(t)
	active := vmConfig{
		ID:           "vm-1",
		UID:          100,
		GID:          100,
		DesiredState: vm.StateRunning,
		Spec: vm.Spec{Network: vm.Network{
			Egress:            vm.EgressMesh,
			WireGuardMeshIPv6: "fdaa:1:0:7::1",
		}},
	}
	destroyed := active
	destroyed.ID = "vm-2"
	destroyed.DesiredState = vm.StateDestroyed
	if err := driver.cfg.writeVMConfig(active); err != nil {
		t.Fatal(err)
	}
	if err := driver.cfg.writeVMConfig(destroyed); err != nil {
		t.Fatal(err)
	}

	if err := driver.RestoreNetworks(context.Background()); err != nil {
		t.Fatal(err)
	}
	if networkAllocator.allocateCalls != 1 {
		t.Fatalf("network allocations = %d, want 1", networkAllocator.allocateCalls)
	}
	if networkAllocator.request.VirtualMachineID != active.ID || networkAllocator.request.WireGuardMeshIPv6 != active.Spec.Network.WireGuardMeshIPv6 {
		t.Fatalf("network request = %+v", networkAllocator.request)
	}
}

func TestUpdateNetworkPersistsAndAppliesSettings(t *testing.T) {
	driver, networkAllocator, _ := testDriver(t)
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{Network: vm.Network{Egress: vm.EgressNone}}); err != nil {
		t.Fatal(err)
	}

	update := vm.NetworkUpdate{
		Egress:                        vm.EgressUplink,
		PublicIPv4:                    "203.0.113.10",
		PrivateNetworkThroughputMiBps: 100,
		PublicNetworkThroughputMiBps:  50,
	}
	if err := driver.UpdateNetwork(context.Background(), "vm-1", update); err != nil {
		t.Fatal(err)
	}
	if networkAllocator.updateRequest.Previous.Egress != vm.EgressNone || networkAllocator.updateRequest.Desired.PublicIPv4 != update.PublicIPv4 {
		t.Fatalf("network update = %+v", networkAllocator.updateRequest)
	}

	configuration, err := driver.cfg.readVMConfig("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if configuration.Spec.Network != (vm.Network{Egress: vm.EgressUplink, PublicIPv4: update.PublicIPv4, PrivateNetworkThroughputMiBps: 100, PublicNetworkThroughputMiBps: 50}) {
		t.Fatalf("network = %+v", configuration.Spec.Network)
	}
}

func TestUpdateNetworkRejectsPublicIPv4WithoutUplink(t *testing.T) {
	driver, networkAllocator, _ := testDriver(t)
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{Network: vm.Network{Egress: vm.EgressUplink}}); err != nil {
		t.Fatal(err)
	}

	err := driver.UpdateNetwork(context.Background(), "vm-1", vm.NetworkUpdate{
		Egress:     vm.EgressMesh,
		PublicIPv4: "203.0.113.10",
	})
	if err == nil {
		t.Fatal("a public IPv4 address without an internet path must fail")
	}
	if networkAllocator.updateRequest.VirtualMachineID != "" {
		t.Fatal("the network must not change when the request is invalid")
	}
}

func TestReplaceSSHKeysUpdatesStoredConfiguration(t *testing.T) {
	driver, _, _ := testDriver(t)
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{SSHKeys: []string{"old"}}); err != nil {
		t.Fatal(err)
	}

	sshKeys := []string{"new-one", "new-two"}
	if err := driver.ReplaceSSHKeys(context.Background(), "vm-1", sshKeys); err != nil {
		t.Fatal(err)
	}
	configuration, err := driver.cfg.readVMConfig("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(configuration.Spec.SSHKeys) != 2 || configuration.Spec.SSHKeys[0] != "new-one" {
		t.Fatalf("ssh keys = %v", configuration.Spec.SSHKeys)
	}
}

func TestResizeComputeUpdatesStoppedVMAndRequestsBoot(t *testing.T) {
	driver, _, _ := testDriver(t)
	if _, err := driver.Create(context.Background(), "vm-1", vm.Spec{VCPUs: 1, MemoryMiB: 128}); err != nil {
		t.Fatal(err)
	}

	if err := driver.ResizeCompute(context.Background(), "vm-1", 2, 256); err != nil {
		t.Fatal(err)
	}
	configuration, err := driver.cfg.readVMConfig("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if configuration.Spec.VCPUs != 2 || configuration.Spec.MemoryMiB != 256 {
		t.Fatalf("compute = %d vCPUs and %d MiB", configuration.Spec.VCPUs, configuration.Spec.MemoryMiB)
	}
	if configuration.DesiredState != vm.StateRunning {
		t.Fatalf("desired state = %q, want running", configuration.DesiredState)
	}
}

func TestDestroyRetainsTombstoneUntilCleanupSucceeds(t *testing.T) {
	driver, networkAllocator, images := testDriver(t)
	configuration := vmConfig{
		ID:           "vm-1",
		UID:          100,
		DesiredState: vm.StateRunning,
		Spec:         vm.Spec{Network: vm.Network{WireGuardMeshIPv6: "fdaa:1:0:7::1"}},
	}
	if err := driver.cfg.writeVMConfig(configuration); err != nil {
		t.Fatal(err)
	}
	images.releaseError = errors.New("storage busy")
	machine := driver.newMachine(configuration)

	if err := machine.Destroy(context.Background()); err == nil {
		t.Fatal("destroy succeeded while storage cleanup failed")
	}
	retained, err := driver.cfg.readVMConfig("vm-1")
	if err != nil {
		t.Fatalf("read retained tombstone: %v", err)
	}
	if retained.DesiredState != vm.StateDestroyed {
		t.Fatalf("desired state = %q, want destroyed", retained.DesiredState)
	}
	if !retained.Cleanup.Systemd || !retained.Cleanup.Network || retained.Cleanup.Storage {
		t.Fatalf("cleanup state = %+v", retained.Cleanup)
	}

	images.releaseError = nil
	if err := machine.Destroy(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.cfg.readVMConfig("vm-1"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("config after destroy = %v, want ErrNotFound", err)
	}
	if networkAllocator.releaseCalls != 1 {
		t.Fatalf("network releases = %d, want 1", networkAllocator.releaseCalls)
	}
	// The mesh registration names the host veth, so release needs both values.
	wanted := network.ReleaseRequest{VirtualMachineID: "vm-1", UserID: 100, WireGuardMeshIPv6: "fdaa:1:0:7::1"}
	if networkAllocator.releaseRequest != wanted {
		t.Fatalf("release request = %+v, want %+v", networkAllocator.releaseRequest, wanted)
	}
}

func TestMetadataServiceData(t *testing.T) {
	data := metadataServiceData("vm1", "10.20.3.14", "06:8f:2a:1b:44:e0", vm.Spec{
		SSHKeys:  []string{"ssh-ed25519 AAAA...", "ssh-rsa BBBB..."},
		Hostname: "worker-1",
		UserData: "#cloud-config\npackages: [curl]",
		Network:  vm.Network{PublicIPv4: "203.0.113.7", WireGuardMeshIPv6: "fdaa:1:0:7::1"},
		Metadata: map[string]string{"env": "prod", "team": "platform"},
	})
	publicKeys := data["latest"].(map[string]any)["meta-data"].(map[string]any)["public-keys"].(map[string]any)
	if got := publicKeys["0"].(map[string]any)["openssh-key"]; got != "ssh-ed25519 AAAA..." {
		t.Errorf("key 0 = %v", got)
	}
	if got := publicKeys["1"].(map[string]any)["openssh-key"]; got != "ssh-rsa BBBB..." {
		t.Errorf("key 1 = %v", got)
	}
	metadata := data["latest"].(map[string]any)["meta-data"].(map[string]any)
	if got := metadata["instance-id"]; got != "vm1" {
		t.Errorf("instance id = %v", got)
	}
	if got := metadata["local-hostname"]; got != "worker-1" {
		t.Errorf("hostname = %v", got)
	}
	if got := metadata["local-ipv4"]; got != "10.20.3.14" {
		t.Errorf("local ipv4 = %v", got)
	}
	if got := metadata["mac"]; got != "06:8f:2a:1b:44:e0" {
		t.Errorf("mac = %v", got)
	}
	if got := metadata["public-ipv4"]; got != "203.0.113.7" {
		t.Errorf("public ipv4 = %v", got)
	}
	if got := metadata["mesh-ipv6"]; got != "fdaa:1:0:7::1" {
		t.Errorf("mesh ipv6 = %v", got)
	}
	customMetadata := metadata["attributes"].(map[string]any)
	if got := customMetadata["env"]; got != "prod" {
		t.Errorf("metadata env = %v", got)
	}
	if got := customMetadata["team"]; got != "platform" {
		t.Errorf("metadata team = %v", got)
	}
	if got := data["latest"].(map[string]any)["user-data"]; got != "#cloud-config\npackages: [curl]" {
		t.Errorf("user data = %v", got)
	}
}

func TestResourceLimitsIncludeSnapshotMemory(t *testing.T) {
	limits := resourceLimits(vm.Spec{VCPUs: 2, MemoryMiB: 512})
	expectedMemory := int64(2*512+128) << 20
	if limits.MemoryMaxBytes != expectedMemory {
		t.Errorf("memory = %d, want %d", limits.MemoryMaxBytes, expectedMemory)
	}
	if limits.CPUQuotaPct != 200 {
		t.Errorf("CPU quota = %d", limits.CPUQuotaPct)
	}
}
