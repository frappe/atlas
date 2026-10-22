package migration

import (
	"bytes"
	"errors"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func kernelDestination(machines *fakeMigrationHost) destinationRecord {
	machines.desired["vm-1"] = vm.DesiredRecord{ID: "vm-1", Specification: testSpecification()}
	return destinationRecord{ID: "mig-1", VirtualMachineID: "vm-1"}
}

func TestReceiveKernelSkipsAStoredKernel(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)

	if err := migrationManager.receiveKernel(t.Context(), kernelDestination(machines)); err != nil {
		t.Fatal(err)
	}
	if source.kernelCalls != 0 {
		t.Fatalf("kernel requests = %d", source.kernelCalls)
	}
}

func TestReceiveKernelStoresAMissingKernel(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	kernels := &fakeKernelStore{isMissing: true}
	migrationManager.kernels = kernels
	source.kernel = []byte("vmlinux")

	if err := migrationManager.receiveKernel(t.Context(), kernelDestination(machines)); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(kernels.stored, source.kernel) {
		t.Fatalf("stored kernel = %q", kernels.stored)
	}
}

func TestReceiveKernelContinuesWhenTheSourceHasNoKernel(t *testing.T) {
	migrationManager, machines, source := newMigrationManager(t)
	kernels := &fakeKernelStore{isMissing: true}
	migrationManager.kernels = kernels

	if err := migrationManager.receiveKernel(t.Context(), kernelDestination(machines)); err != nil {
		t.Fatal(err)
	}
	if source.kernelCalls != 1 || kernels.stored != nil {
		t.Fatalf("kernel requests = %d, stored = %q", source.kernelCalls, kernels.stored)
	}
}

func TestOpenSourceKernelRefusesAnotherMigration(t *testing.T) {
	migrationManager, _, _ := newMigrationManager(t)
	if err := migrationManager.store.writeSource(sourceRecord{ID: "mig-1", VirtualMachineID: "vm-1", State: sourceLocked, Sequence: 1}); err != nil {
		t.Fatal(err)
	}

	if _, err := migrationManager.OpenSourceKernel(t.Context(), "mig-2", "vm-1"); !errors.Is(err, vm.ErrConflict) {
		t.Fatalf("error = %v", err)
	}
}
