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

type testHostDependencies struct {
	privilegedAddresses []string
	wireGuardPeers      []network.WireGuardPeer
	unicast             bool
	peerSyncs           int
	images              []vm.Image
	virtualMachines     []vm.Information
	wakeCount           int
}

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

func (dependencies *testHostDependencies) List(context.Context) ([]vm.Information, error) {
	return append([]vm.Information(nil), dependencies.virtualMachines...), nil
}

func (dependencies *testHostDependencies) Capacity(context.Context) (storage.Capacity, error) {
	return storage.Capacity{TotalMiB: 4096, AvailableMiB: 3072}, nil
}

func TestSynchronizeAppliesControllerStateAndReportsCapacity(t *testing.T) {
	dependencies := &testHostDependencies{
		virtualMachines: []vm.Information{{
			ID: "vm-00001", State: vm.StateRunning, CPUMillicores: 1500,
			Routes: []vm.Route{{Destination: "fdac:1:1::/48", Via: "fdaa:1::1", Scope: vm.RouteScopeWireGuardGateway}},
		}},
	}
	service, err := NewService(Dependencies{
		Mesh: dependencies, WireGuard: dependencies, Images: dependencies,
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
	}
	result, err := service.Synchronize(t.Context(), desired)
	if err != nil {
		t.Fatal(err)
	}

	if len(dependencies.privilegedAddresses) != 1 || len(dependencies.wireGuardPeers) != 1 || len(dependencies.images) != 1 {
		t.Fatalf("controller state was not applied: %+v", dependencies)
	}
	if dependencies.wakeCount != 1 {
		t.Fatalf("wake count = %d, want 1", dependencies.wakeCount)
	}
	capacity := result.Capacity
	if capacity.AvailableCPUMillicores != max(runtime.NumCPU()*1000-1500, 0) || capacity.TotalStorageMiB != 4096 || capacity.AvailableStorageMiB != 3072 {
		t.Fatalf("capacity = %+v", capacity)
	}
	report := result.VirtualMachines["vm-00001"]
	if report.State != vm.StateRunning || len(report.Routes) != 1 || report.Routes[0].Scope != vm.RouteScopeWireGuardGateway {
		t.Fatalf("virtual machine reports = %+v", result.VirtualMachines)
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
		Mesh: dependencies, WireGuard: dependencies, Images: dependencies,
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
		WireGuard: dependencies, Images: dependencies, VirtualMachines: dependencies, Storage: dependencies,
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
			WireGuard: dependencies, Images: dependencies, VirtualMachines: dependencies, Storage: dependencies,
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
		WireGuard: dependencies, Images: dependencies, VirtualMachines: dependencies, Storage: dependencies,
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
