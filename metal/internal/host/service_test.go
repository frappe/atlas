package host

import (
	"context"
	"runtime"
	"testing"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/frappe/atlas/metal/internal/vm/migration"
)

func TestMissingCompatibilityPreservesHostSync(t *testing.T) {
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{ID: "vm-1", State: vm.StateRunning}},
	}
	service, err := NewService(Dependencies{
		WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies,
		Wake: func() { dependencies.wakeCount++ },
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := service.Synchronize(t.Context(), DesiredState{})
	if err != nil || result.Compatibility != nil {
		t.Fatalf("sync result = %+v, error = %v", result, err)
	}
	if result.Capacity.TotalStorageMiB != 4096 || result.VirtualMachines["vm-1"].State != vm.StateRunning || dependencies.wakeCount != 1 {
		t.Fatalf("probe failure interrupted host sync: %+v", result)
	}
}

type testHostDependencies struct {
	privilegedAddresses []string
	wireGuardPeers      []network.WireGuardPeer
	unicast             bool
	peerSyncs           int
	images              []vm.Image
	virtualMachines     []vm.Information
	invalid             []string
	gatewayRoutes       []vm.Route
	wakeCount           int
}

func (*testHostDependencies) EnsureHooks(context.Context) error { return nil }

func (dependencies *testHostDependencies) ApplyPrivilegedAddresses(_ context.Context, addresses []string) error {
	dependencies.privilegedAddresses = append([]string(nil), addresses...)
	return nil
}

func (dependencies *testHostDependencies) Apply(_ context.Context, peers []network.WireGuardPeer) error {
	dependencies.wireGuardPeers = append([]network.WireGuardPeer(nil), peers...)
	return nil
}

func (dependencies *testHostDependencies) SetImagePolicies(_ context.Context, images []vm.Image) error {
	dependencies.images = append([]vm.Image(nil), images...)
	return nil
}

func (dependencies *testHostDependencies) SyncPeerState(_ context.Context, unicast bool) error {
	dependencies.unicast = unicast
	dependencies.peerSyncs++
	return nil
}

func (dependencies *testHostDependencies) PrivateNetworkMAC() (string, error) {
	return "02:00:00:00:00:01", nil
}

func (dependencies *testHostDependencies) List(context.Context) ([]vm.Information, []string, error) {
	return append([]vm.Information(nil), dependencies.virtualMachines...), dependencies.invalid, nil
}

func (dependencies *testHostDependencies) SetWireGuardGatewayRoutes(_ context.Context, routes []vm.Route) error {
	dependencies.gatewayRoutes = append([]vm.Route(nil), routes...)
	return nil
}

func (dependencies *testHostDependencies) Capacity(context.Context) (storage.Capacity, error) {
	return storage.Capacity{TotalMiB: 4096, AvailableMiB: 3072}, nil
}

func TestSynchronizeAppliesControllerStateAndReportsCapacity(t *testing.T) {
	compatibility := vm.HostCompatibility{Architecture: "amd64", CPUProfile: "builtin:baseline"}
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{ID: "vm-00001", State: vm.StateRunning, CPUMillicores: 1500}},
	}
	service, err := NewService(Dependencies{
		Compatibility: &compatibility,
		Mesh:          dependencies, WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies,
		Wake: func() { dependencies.wakeCount++ },
	})
	if err != nil {
		t.Fatal(err)
	}

	desired := DesiredState{
		PrivilegedVirtualMachineAddresses: []string{"fdaa::2"},
		WireGuardPeers:                    []network.WireGuardPeer{{Node: "node-2"}},
		Images:                            []vm.Image{{Name: "ubuntu"}},
		WireGuardGatewayRoutes:            []vm.Route{{Destination: "fdac:1:1::/48", Via: "fdaa:1::1"}},
	}
	result, err := service.Synchronize(t.Context(), desired)
	if err != nil {
		t.Fatal(err)
	}

	if len(dependencies.privilegedAddresses) != 1 || len(dependencies.wireGuardPeers) != 1 ||
		len(dependencies.images) != 1 || len(dependencies.gatewayRoutes) != 1 {
		t.Fatalf("controller state was not applied: %+v", dependencies)
	}
	if dependencies.wakeCount != 1 {
		t.Fatalf("wake count = %d, want 1", dependencies.wakeCount)
	}
	capacity := result.Capacity
	if capacity.AvailableCPUMillicores != max(runtime.NumCPU()*1000-1500, 0) || capacity.TotalStorageMiB != 4096 || capacity.AvailableStorageMiB != 3072 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if result.VirtualMachines["vm-00001"].State != vm.StateRunning {
		t.Fatalf("virtual machines = %+v", result.VirtualMachines)
	}
	if result.Compatibility == nil || *result.Compatibility != compatibility {
		t.Fatalf("compatibility = %+v", result.Compatibility)
	}
}

func TestCapacityCountsProvisionedDisks(t *testing.T) {
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{ID: "vm-00001", State: vm.StateRunning, DiskMiB: 3072}},
	}
	service, err := NewService(Dependencies{
		WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies, Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}

	capacity, err := service.Capacity(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	// The thin disk uses little pool space now, but it can grow to 3072 MiB.
	if capacity.AvailableStorageMiB != 4096-3072 {
		t.Fatalf("available storage = %d, want 1024", capacity.AvailableStorageMiB)
	}
}

func TestAnUnreadableRecordStopsPlacementButNotSync(t *testing.T) {
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{ID: "vm-00001", State: vm.StateRunning}},
		invalid:         []string{"vm-00002"},
	}
	service, err := NewService(Dependencies{
		WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies, Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}

	result, err := service.Synchronize(t.Context(), DesiredState{})
	if err != nil {
		t.Fatal(err)
	}
	// The unreadable VM holds an unknown shape, so the host offers no capacity.
	capacity := result.Capacity
	if capacity.AvailableCPUMillicores != 0 || capacity.AvailableMemoryMiB != 0 || capacity.AvailableStorageMiB != 0 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if result.VirtualMachines["vm-00001"].State != vm.StateRunning || len(result.InvalidVirtualMachineIDs) != 1 {
		t.Fatalf("sync result = %+v", result)
	}
}

func TestCapacitySubtractsMigrationReservations(t *testing.T) {
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{ID: "vm-00001", State: vm.StateRunning, CPUMillicores: 1500}},
	}
	reservations := func(context.Context) ([]migration.DestinationReservation, error) {
		return []migration.DestinationReservation{{VirtualMachineID: "vm-00002", CPUMillicores: 2500, MemoryMiB: 1024, DiskMiB: 2048}}, nil
	}
	service, err := NewService(Dependencies{
		Mesh: dependencies, WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies,
		MigrationReservations: reservations, Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}

	capacity, err := service.Capacity(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AvailableCPUMillicores != max(runtime.NumCPU()*1000-1500-2500, 0) {
		t.Fatalf("available CPU = %d", capacity.AvailableCPUMillicores)
	}
	if capacity.AvailableStorageMiB != 3072-2048 {
		t.Fatalf("available storage = %d, want 1024", capacity.AvailableStorageMiB)
	}
	// Migration destinations are not running VMs.
	if capacity.VirtualMachineCount != 1 {
		t.Fatalf("VM count = %d, want 1", capacity.VirtualMachineCount)
	}
}

func TestSynchronizeAllowsMeshToBeDisabled(t *testing.T) {
	dependencies := &testHostDependencies{}
	service, err := NewService(Dependencies{
		WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies,
		Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Synchronize(t.Context(), DesiredState{}); err != nil {
		t.Fatal(err)
	}
}

func TestSynchronizeSelectsTheNDPMode(t *testing.T) {
	for _, unicast := range []bool{true, false} {
		dependencies := &testHostDependencies{}
		service, err := NewService(Dependencies{
			WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
			VirtualMachines: dependencies, Storage: dependencies,
			Mesh: dependencies, Wake: func() {},
		})
		if err != nil {
			t.Fatal(err)
		}

		if _, err := service.Synchronize(t.Context(), DesiredState{UnicastEnabled: unicast}); err != nil {
			t.Fatal(err)
		}

		if dependencies.unicast != unicast || dependencies.peerSyncs != 1 {
			t.Fatalf("unicast = %t, peer syncs = %d, want unicast %t", dependencies.unicast, dependencies.peerSyncs, unicast)
		}
	}
}

func TestSynchronizeRefusesUnicastWithoutTheMesh(t *testing.T) {
	dependencies := &testHostDependencies{}
	service, err := NewService(Dependencies{
		WireGuard: dependencies, Images: dependencies, GatewayRoutes: dependencies,
		VirtualMachines: dependencies, Storage: dependencies,
		Wake: func() {},
	})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := service.Synchronize(t.Context(), DesiredState{UnicastEnabled: true}); err == nil {
		t.Fatal("unicast sync without the mesh succeeded")
	}
	if dependencies.wireGuardPeers != nil {
		t.Fatal("WireGuard peers were applied before the refusal")
	}
}
