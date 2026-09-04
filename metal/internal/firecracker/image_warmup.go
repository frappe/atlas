package firecracker

import (
	"context"
	"fmt"
	"log"
	"path/filepath"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// EnsureMemorySnapshot builds the requested local memory snapshot when needed.
func (d *Driver) EnsureMemorySnapshot(ctx context.Context, image vm.ImageRef) error {
	configuration := image.MemorySnapshotConfiguration
	if !image.CacheImage || !image.MemorySnapshot || configuration == nil {
		return nil
	}
	if err := d.imageStore.EnsureImage(ctx, image); err != nil {
		return err
	}

	compatibility := d.firecrackerCompatibility()
	key := storage.WarmImageKey(image, *configuration, compatibility)
	if err := d.imageStore.RemoveOtherWarmImages(ctx, image.Name, key); err != nil {
		return err
	}
	if _, found, err := d.imageStore.WarmImage(ctx, image, *configuration, compatibility); err != nil || found {
		return err
	}

	return d.buildMemorySnapshot(ctx, image, *configuration, compatibility, key)
}

func (d *Driver) buildMemorySnapshot(
	ctx context.Context,
	image vm.ImageRef,
	configuration vm.MemorySnapshotConfiguration,
	compatibility string,
	key string,
) error {
	virtualMachineID := "warm-" + key[:32]
	specification := warmupSpecification(image, configuration)
	virtualMachine, err := d.Create(ctx, virtualMachineID, specification)
	if err != nil {
		return fmt.Errorf("create warmup virtual machine: %w", err)
	}
	machine, ok := virtualMachine.(*machine)
	if !ok {
		return fmt.Errorf("create warmup virtual machine: unexpected driver result")
	}
	defer func() {
		if err := machine.Destroy(context.WithoutCancel(ctx)); err != nil {
			log.Printf("firecracker: remove warmup vm %s: %v", virtualMachineID, err)
		}
	}()

	if err := d.Reconcile(ctx, virtualMachineID); err != nil {
		return fmt.Errorf("start warmup virtual machine: %w", err)
	}
	if err := waitForWarmup(ctx, d.warmupDelay); err != nil {
		return err
	}
	if err := machine.Pause(ctx); err != nil {
		return fmt.Errorf("pause warmup virtual machine: %w", err)
	}

	const snapshotName = "warm"
	if err := d.imageStore.CreateWarmSourceSnapshot(ctx, virtualMachineID, snapshotName); err != nil {
		return fmt.Errorf("snapshot warmup disk: %w", err)
	}
	defer d.imageStore.DeleteWarmSourceSnapshot(context.WithoutCancel(ctx), virtualMachineID, snapshotName)

	stateFile, memoryFile, err := d.createWarmupMemoryFiles(ctx, machine, virtualMachineID)
	if err != nil {
		return err
	}
	_, err = d.imageStore.PromoteWarmSnapshot(ctx, storage.WarmImagePromotion{
		Image:                    image,
		Configuration:            configuration,
		FirecrackerCompatibility: compatibility,
		SourceVirtualMachineID:   virtualMachineID,
		SourceSnapshotName:       snapshotName,
		StateFile:                stateFile,
		MemoryFile:               memoryFile,
	})
	if err != nil {
		return fmt.Errorf("store warm image: %w", err)
	}
	return nil
}

func (d *Driver) createWarmupMemoryFiles(
	ctx context.Context,
	machine *machine,
	virtualMachineID string,
) (string, string, error) {
	directory := filepath.Join(d.cfg.chrootRoot(virtualMachineID), "warm")
	if err := mkdirChown(directory, machine.cfg.UID, machine.cfg.GID); err != nil {
		return "", "", err
	}

	if err := machine.api.CreateSnapshot(ctx, api.CreateSnapshotRequest{
		SnapshotType: "Full",
		SnapshotPath: "warm/state",
		MemoryFile:   "warm/memory",
	}); err != nil {
		return "", "", fmt.Errorf("create warmup memory snapshot: %w", err)
	}
	return filepath.Join(directory, "state"), filepath.Join(directory, "memory"), nil
}

func warmupSpecification(image vm.ImageRef, configuration vm.MemorySnapshotConfiguration) vm.Spec {
	image.MemorySnapshot = false
	image.MemorySnapshotConfiguration = nil
	return vm.Spec{
		VCPUs:     configuration.VirtualCPUCount,
		MemoryMiB: configuration.MemoryMiB,
		DiskMiB:   configuration.DiskMiB,
		Image:     image,
		Network:   vm.Network{Egress: vm.EgressNone},
	}
}

func waitForWarmup(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
