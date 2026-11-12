package vm

import (
	"context"
	"errors"
	"os"
	"testing"
)

func TestReconcileSkipsVirtualMachineRemovedAfterListing(t *testing.T) {
	ctx := context.Background()
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(ctx, "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := manager.Delete(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(manager.store.virtualMachineDirectory("machine-1")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("records remain after destroy: %v", err)
	}

	operations := map[string]func(context.Context, string) error{
		"reconcile":     manager.Reconcile,
		"sleep restore": manager.RestoreSleepState,
		"sleep upload":  manager.OffloadSleepArtifacts,
	}
	for name, operation := range operations {
		if err := operation(ctx, "machine-1"); err != nil {
			t.Errorf("%s of a removed VM failed: %v", name, err)
		}
	}
}

func TestReconcileFailsWhenObservedRecordIsMissing(t *testing.T) {
	ctx := context.Background()
	manager, _, _, _ := newTestManager(t)
	if _, err := manager.Create(ctx, "machine-1", testSpecification()); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(manager.store.observedPath("machine-1")); err != nil {
		t.Fatal(err)
	}
	if err := manager.Reconcile(ctx, "machine-1"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}
