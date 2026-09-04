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
	allocateCalls int
	releaseCalls  int
	releaseError  error
}

func (f *fakeNetwork) Allocate(context.Context, network.Request) (network.Interface, error) {
	f.allocateCalls++
	return network.Interface{GuestIPAddress: "172.16.0.2", MACAddress: "02:00:00:00:00:01"}, nil
}

func (f *fakeNetwork) Resolve(string) network.Interface { return network.Interface{} }

func (f *fakeNetwork) Release(context.Context, string) error {
	f.releaseCalls++
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
	configuration := vmConfig{ID: "vm-1", DesiredState: vm.StateRunning}
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
}

func TestMetadataServiceData(t *testing.T) {
	data := metadataServiceData("vm1", vm.Spec{
		SSHKeys:  []string{"ssh-ed25519 AAAA...", "ssh-rsa BBBB..."},
		Hostname: "worker-1",
		UserData: "#cloud-config\npackages: [curl]",
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
