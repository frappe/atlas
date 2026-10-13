package storage

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/google/uuid"
)

// TestRescueCheckpointZFS requires root and an explicitly selected disposable
// host pool. It never uses an existing VM disk.
func TestRescueCheckpointZFS(t *testing.T) {
	pool := os.Getenv("ATLAS_RESCUE_TEST_POOL")
	if pool == "" {
		t.Skip("set ATLAS_RESCUE_TEST_POOL on a disposable ZFS host")
	}
	ctx := context.Background()
	store := &VirtualMachineStore{pool: &ZFSPool{name: pool}}
	id := "rescue-check-" + uuid.NewString()
	source := store.pool.virtualMachineDataset(id + "-source")
	dataset := store.pool.virtualMachineDataset(id)
	if err := platform.Run(ctx, "zfs", "create", "-s", "-V", "64M", source); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := platform.Run(ctx, "zfs", "destroy", "-r", source); err != nil {
			t.Error(err)
		}
	})
	if err := platform.Run(ctx, "zfs", "snapshot", source+"@image"); err != nil {
		t.Fatal(err)
	}
	if err := platform.Run(ctx, "zfs", "clone", source+"@image", dataset); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := platform.Run(ctx, "zfs", "destroy", "-r", dataset); err != nil {
			t.Error(err)
		}
	})
	if err := platform.Run(ctx, "udevadm", "settle"); err != nil {
		t.Fatal(err)
	}
	disk, err := os.OpenFile(store.pool.virtualMachineDevicePath(id), os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer disk.Close()
	write := func(value string) {
		t.Helper()
		if _, err := disk.WriteAt([]byte(value), 0); err != nil {
			t.Fatal(err)
		}
		if err := disk.Sync(); err != nil {
			t.Fatal(err)
		}
	}
	write("before")
	guid, err := store.CreateRescueCheckpoint(ctx, id, 7)
	if err != nil {
		t.Fatal(err)
	}
	write("after!")
	adopted, err := store.CreateRescueCheckpoint(ctx, id, 7)
	if err != nil || adopted != guid {
		t.Fatalf("retry = %s, %v", adopted, err)
	}
	if err := platform.Run(ctx, "zfs", "snapshot", dataset+"@newer"); err != nil {
		t.Fatal(err)
	}
	// Stopping Firecracker closes its disk descriptor before rollback.
	if err := disk.Close(); err != nil {
		t.Fatal(err)
	}
	if err := store.RollbackRescueCheckpoint(ctx, id, 7, guid); err == nil {
		t.Fatal("rollback crossed a newer snapshot")
	}
	if err := platform.Run(ctx, "zfs", "destroy", dataset+"@newer"); err != nil {
		t.Fatal(err)
	}
	if err := store.RollbackRescueCheckpoint(ctx, id, 7, guid); err != nil {
		t.Fatal(err)
	}
	disk, err = os.Open(store.pool.virtualMachineDevicePath(id))
	if err != nil {
		t.Fatal(err)
	}
	defer disk.Close()
	data := make([]byte, 6)
	if _, err := disk.ReadAt(data, 0); err != nil || string(data) != "before" {
		t.Fatalf("restored = %q, %v", data, err)
	}
	if err := store.DiscardRescueCheckpoint(ctx, id, 7, guid); err != nil {
		t.Fatal(err)
	}
	if err := store.DiscardRescueCheckpoint(ctx, id, 7, guid); err != nil {
		t.Fatal(err)
	}
	if err := store.RollbackRescueCheckpoint(ctx, id, 7, guid); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing checkpoint rollback = %v", err)
	}
	if _, err := store.CreateRescueCheckpoint(ctx, id, 7); err != nil {
		t.Fatal(err)
	}
	if err := store.RollbackRescueCheckpoint(ctx, id, 7, guid); err == nil {
		t.Fatal("rollback accepted a replacement checkpoint")
	}
	if err := store.DiscardRescueCheckpoint(ctx, id, 7, guid); err == nil {
		t.Fatal("cleanup accepted a replacement checkpoint")
	}
}
