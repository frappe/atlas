package firecracker

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// prepareBoot starts a jailed Firecracker process and configures its machine,
// kernel, drives, network, and metadata service for a cold boot.
func (runtime *Runtime) prepareBoot(ctx context.Context, configuration vm.RuntimeMachine) error {
	if err := runtime.prepareLaunch(ctx, configuration); err != nil {
		return err
	}

	bootConfiguration, err := runtime.virtualMachineStorage.PrepareBoot(ctx, storage.VirtualMachineStorageRequest{
		VirtualMachineID: configuration.ID,
		ImageReference:   configuration.Specification.Image.Name,
		Image:            configuration.Specification.Image,
		ChrootRoot:       runtime.configuration.chrootRoot(configuration.ID),
		UserID:           configuration.UserID,
		GroupID:          configuration.GroupID,
		DiskMiB:          configuration.Specification.DiskMiB,
	})
	if err != nil {
		return err
	}
	runtime.logger.Debug("configured Firecracker VM", "virtual_machine_id", configuration.ID, "kernel", bootConfiguration.Kernel, "cmdline", bootArguments(bootConfiguration, configuration.NetworkInterface))
	client := api.New(runtime.configuration.socketPath(configuration.ID))
	if len(runtime.configuration.CPUTemplate) != 0 {
		if err := client.PutCPUConfig(ctx, runtime.configuration.CPUTemplate); err != nil {
			return fmt.Errorf("configure guest CPU: %w", err)
		}
	}
	return configure(
		ctx,
		client,
		configuration.ID,
		configuration.Specification,
		bootConfiguration,
		configuration.NetworkInterface,
	)
}

// prepareLaunch creates the jail and starts the unit. It opens the console PTY
// first so guest output is not lost.
func (runtime *Runtime) prepareLaunch(ctx context.Context, configuration vm.RuntimeMachine) error {
	if err := runtime.configuration.writeJailerEnv(
		configuration.ID,
		runtime.configuration.jailerArgs(
			configuration.ID,
			configuration.UserID,
			configuration.GroupID,
			configuration.NetworkInterface.NetworkNamespacePath,
		),
	); err != nil {
		return err
	}
	if err := runtime.configuration.linkSocket(configuration.ID); err != nil {
		return err
	}
	// Open the PTY before systemd starts the unit.
	if err := runtime.serialBroker.Open(configuration.ID); err != nil {
		return err
	}
	if err := runtime.units.Start(ctx, configuration.ID); err != nil {
		return err
	}
	// The unit holds the slave now, so systemd can keep the master.
	if err := runtime.serialBroker.Persist(configuration.ID); err != nil {
		runtime.logger.Warn(
			"console master not preserved; a metald exit stops this VM",
			"virtual_machine_id", configuration.ID,
			"error", err,
		)
	}
	if err := runtime.units.SetLimits(ctx, configuration.ID, resourceLimits(configuration.Specification)); err != nil {
		return err
	}
	if err := waitSocket(ctx, runtime.configuration.socketPath(configuration.ID)); err != nil {
		return err
	}

	return nil
}

// relaunch discards the old jail and boots from a clean one.
func (runtime *Runtime) relaunch(ctx context.Context, configuration vm.RuntimeMachine) error {
	_ = runtime.units.Stop(ctx, configuration.ID)
	_ = os.RemoveAll(filepath.Dir(runtime.configuration.chrootRoot(configuration.ID)))
	return runtime.prepareBoot(ctx, configuration)
}

// launchMemorySnapshot restores a guest from a memory snapshot, updates metadata
// before resume, and leaves the vCPUs paused when resume is false.
func (runtime *Runtime) launchMemorySnapshot(
	ctx context.Context,
	configuration vm.RuntimeMachine,
	rootSnapshot string,
	stateFile string,
	memoryFile string,
	metadata map[string]any,
	resume bool,
) error {
	_ = runtime.units.Stop(ctx, configuration.ID)
	_ = os.RemoveAll(filepath.Dir(runtime.configuration.chrootRoot(configuration.ID)))

	if err := runtime.prepareLaunch(ctx, configuration); err != nil {
		return err
	}

	if err := runtime.virtualMachineStorage.PrepareRootFileSystem(ctx, storage.VirtualMachineStorageRequest{
		VirtualMachineID: configuration.ID,
		ImageReference:   configuration.Specification.Image.Name,
		Image:            configuration.Specification.Image,
		ChrootRoot:       runtime.configuration.chrootRoot(configuration.ID),
		UserID:           configuration.UserID,
		GroupID:          configuration.GroupID,
		DiskMiB:          configuration.Specification.DiskMiB,
		SourceSnapshot:   rootSnapshot,
	}); err != nil {
		return err
	}

	stage := filepath.Join(runtime.configuration.chrootRoot(configuration.ID), "snap")
	if err := mkdirChown(stage, configuration.UserID, configuration.GroupID); err != nil {
		return err
	}
	if err := copyChown(
		ctx,
		stateFile,
		filepath.Join(stage, "state"),
		configuration.UserID,
		configuration.GroupID,
	); err != nil {
		return err
	}
	if err := copyChown(
		ctx,
		memoryFile,
		filepath.Join(stage, "mem"),
		configuration.UserID,
		configuration.GroupID,
	); err != nil {
		return err
	}

	client := api.New(runtime.configuration.socketPath(configuration.ID))
	if err := client.LoadSnapshot(ctx, api.LoadSnapshotRequest{
		SnapshotPath: "snap/state",
		Memory:       api.MemoryBackend{Path: "snap/mem", Type: "File"},
		Resume:       false,
	}); err != nil {
		return err
	}
	if metadata != nil {
		if err := client.PutMMDS(ctx, metadata); err != nil {
			return fmt.Errorf("refresh snapshot metadata: %w", err)
		}
	}
	if !resume {
		return nil
	}

	return client.Resume(ctx)
}

// launchWarmImage restores a matching warm image or returns ErrNotFound, which
// lets the caller use the reliable cold-boot path.
func (runtime *Runtime) launchWarmImage(ctx context.Context, configuration vm.RuntimeMachine, imageReference string) error {
	memorySnapshotConfiguration := configuration.Specification.Image.MemorySnapshotConfiguration
	if memorySnapshotConfiguration == nil || configuration.Specification.Image.Name != imageReference {
		return vm.ErrNotFound
	}

	artifacts, found, err := runtime.imageStore.WarmImage(
		ctx,
		configuration.Specification.Image,
		*memorySnapshotConfiguration,
		runtime.firecrackerCompatibility(),
	)
	if err != nil {
		return err
	}
	if !found {
		return vm.ErrNotFound
	}

	metadata := configuration.Specification.MetadataServiceData(
		configuration.ID, configuration.NetworkInterface.GuestIPAddress, configuration.NetworkInterface.MACAddress,
	)
	if err := runtime.launchMemorySnapshot(
		ctx,
		configuration,
		artifacts.RootSnapshot,
		artifacts.StateFile,
		artifacts.MemoryFile,
		metadata,
		false,
	); err != nil {
		return err
	}
	// The guest still sees the warm disk size and no disk limits until the drive is refreshed.
	if err := runtime.RefreshDisk(ctx, configuration); err != nil {
		return fmt.Errorf("refresh warm disk: %w", err)
	}

	return api.New(runtime.configuration.socketPath(configuration.ID)).Resume(ctx)
}

// firecrackerCompatibility identifies the build and CPU template for snapshot matching.
// Size and modification time stand in for a version the binary does not report.
func (runtime *Runtime) firecrackerCompatibility() string {
	compatibility := runtime.configuration.FirecrackerBin
	information, err := os.Stat(runtime.configuration.FirecrackerBin)
	if err == nil {
		compatibility = fmt.Sprintf("%s:%d:%d", compatibility, information.Size(), information.ModTime().UnixNano())
	}
	if len(runtime.configuration.CPUTemplate) != 0 {
		compatibility += fmt.Sprintf(":cpu:%x", sha256.Sum256(runtime.configuration.CPUTemplate))
	}
	return compatibility
}

// hasMatchingMemorySnapshot reports whether CPU and memory match the warm image
// shape exactly. A larger disk also matches, because the warm launch shows the
// guest the grown drive.
func (runtime *Runtime) hasMatchingMemorySnapshot(specification vm.Specification) bool {
	configuration := specification.Image.MemorySnapshotConfiguration
	return specification.Image.CacheImage &&
		specification.Image.MemorySnapshot &&
		configuration != nil &&
		configuration.VirtualCPUCount == specification.VirtualCPUCount() &&
		configuration.MemoryMiB == specification.MemoryMiB &&
		configuration.DiskMiB <= specification.DiskMiB
}

// mkdirChown creates a directory the jailed process can write to.
func mkdirChown(path string, userID, groupID uint32) error {
	if err := os.MkdirAll(path, 0o750); err != nil {
		return err
	}
	return os.Chown(path, int(userID), int(groupID))
}

// copyChown places a file inside the jail and gives it to the VM user.
func copyChown(
	ctx context.Context,
	source string,
	destination string,
	userID uint32,
	groupID uint32,
) error {
	if err := storage.LinkOrCopy(ctx, source, destination); err != nil {
		return err
	}
	return os.Chown(destination, int(userID), int(groupID))
}
