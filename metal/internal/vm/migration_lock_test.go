package vm

import (
	"context"
	"errors"
	"testing"
)

// fakeMigrationGuard answers the migration lock questions in a test.
type fakeMigrationGuard struct {
	sourceLocked        map[string]bool
	destinationReserved map[string]bool
}

func (g *fakeMigrationGuard) IsSourceLocked(virtualMachineID string) bool {
	return g.sourceLocked[virtualMachineID]
}

func (g *fakeMigrationGuard) IsDestinationReserved(virtualMachineID string) bool {
	return g.destinationReserved[virtualMachineID]
}

// lockSource installs a guard that reports one VM as a migration source.
func lockSource(t *testing.T, manager *Manager, virtualMachineID string) *fakeMigrationGuard {
	t.Helper()
	guard := &fakeMigrationGuard{
		sourceLocked:        map[string]bool{virtualMachineID: true},
		destinationReserved: map[string]bool{},
	}
	manager.SetMigrationGuard(guard)
	return guard
}

func TestSourceLockBlocksMutationsButAllowsReads(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := manager.Create(ctx, "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	lockSource(t, manager, "vm-1")

	for name, mutate := range map[string]func() error{
		"power":   func() error { return manager.SetPowerState(ctx, "vm-1", StateStopped) },
		"restart": func() error { return manager.RequestRestart(ctx, "vm-1") },
		"network": func() error {
			return manager.SetNetwork(ctx, "vm-1", NetworkConfiguration{WireGuardMeshIPv6: "fdaa::2"})
		},
		"delete": func() error { return manager.Delete(ctx, "vm-1") },
	} {
		if err := mutate(); !errors.Is(err, ErrConflict) {
			t.Fatalf("%s mutation during migration = %v, want ErrConflict", name, err)
		}
	}

	if _, err := manager.Information(ctx, "vm-1"); err != nil {
		t.Fatalf("read during migration = %v, want nil", err)
	}
}

func TestSourceLockSkipsReconciliation(t *testing.T) {
	manager, _, network, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := manager.Create(ctx, "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	lockSource(t, manager, "vm-1")

	if err := manager.Reconcile(ctx, "vm-1"); err != nil {
		t.Fatalf("reconcile of a locked source = %v, want nil", err)
	}
	if network.ensures != 0 {
		t.Fatalf("reconcile touched the network %d times, want 0", network.ensures)
	}
}

func TestClearedSourceLockRestoresMutations(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	ctx := context.Background()
	if _, err := manager.Create(ctx, "vm-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	guard := lockSource(t, manager, "vm-1")
	guard.sourceLocked["vm-1"] = false

	if err := manager.SetPowerState(ctx, "vm-1", StateStopped); err != nil {
		t.Fatalf("power change after unlock = %v, want nil", err)
	}
	if manager.isSourceLocked("vm-1") {
		t.Fatal("source lock still present after clear")
	}
}

func TestKernelSHA256sIncludesAnIncomingMigration(t *testing.T) {
	manager, _, _, _ := newTestManager(t)
	ctx := context.Background()
	specification := testSpecification()
	specification.Image.KernelSHA256 = "kernel"
	if _, err := manager.Create(ctx, "vm-1", specification); err != nil {
		t.Fatal(err)
	}
	manager.SetMigrationGuard(&fakeMigrationGuard{destinationReserved: map[string]bool{"vm-1": true}})

	kernels, err := manager.KernelSHA256s(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(kernels) != 1 || kernels[0] != "kernel" {
		t.Fatalf("kernels = %v", kernels)
	}
}
