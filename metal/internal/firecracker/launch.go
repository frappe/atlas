package firecracker

import (
	"context"
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

	var bootConfiguration storage.BootConfiguration
	var err error
	if configuration.Specification.Rescue.Enabled {
		bootConfiguration, err = runtime.virtualMachineStorage.PrepareRescueBoot(ctx, storage.VirtualMachineRescueBootRequest{
			VirtualMachineID:     configuration.ID,
			RescueGeneration:     configuration.RescueGeneration,
			RescueImageReference: configuration.Specification.Rescue.Image.Name,
			RescueImage:          configuration.Specification.Rescue.Image,
			ChrootRoot:           runtime.configuration.chrootRoot(configuration.ID),
			UserID:               configuration.UserID,
			GroupID:              configuration.GroupID,
		})
	} else {
		bootConfiguration, err = runtime.virtualMachineStorage.PrepareBoot(ctx, storage.VirtualMachineStorageRequest{
			VirtualMachineID: configuration.ID,
			ImageReference:   configuration.Specification.Image.Name,
			Image:            configuration.Specification.Image,
			ChrootRoot:       runtime.configuration.chrootRoot(configuration.ID),
			UserID:           configuration.UserID,
			GroupID:          configuration.GroupID,
			DiskMiB:          configuration.Specification.DiskMiB,
		})
	}
	if err != nil {
		return err
	}
	runtime.logger.Debug("configured Firecracker VM", "virtual_machine_id", configuration.ID, "kernel", bootConfiguration.Kernel, "cmdline", bootArguments(bootConfiguration, configuration.NetworkInterface))
	if err := configure(
		ctx,
		api.New(runtime.configuration.socketPath(configuration.ID)),
		configuration.ID,
		configuration.Specification,
		bootConfiguration,
		configuration.NetworkInterface,
	); err != nil {
		return err
	}
	if configuration.Specification.Rescue.Enabled {
		if err := runtime.prepareRescueListener(configuration); err != nil {
			return err
		}
		return api.New(runtime.configuration.socketPath(configuration.ID)).PutVsock(ctx, api.Vsock{GuestCID: 3, UDSPath: "/rescue.vsock"})
	}
	return nil
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
	if err := runtime.clearRescueBoot(configuration.ID); err != nil {
		return err
	}

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
			runtime.logger.Error("MMDS refresh failed", "virtual_machine_id", configuration.ID, "error", err)
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
	return runtime.launchMemorySnapshot(
		ctx,
		configuration,
		artifacts.RootSnapshot,
		artifacts.StateFile,
		artifacts.MemoryFile,
		metadata,
		true,
	)
}

// firecrackerCompatibility identifies a Firecracker build for snapshot matching.
// Size and modification time stand in for a version the binary does not report.
func (runtime *Runtime) firecrackerCompatibility() string {
	information, err := os.Stat(runtime.configuration.FirecrackerBin)
	if err != nil {
		return runtime.configuration.FirecrackerBin
	}
	return fmt.Sprintf(
		"%s:%d:%d",
		runtime.configuration.FirecrackerBin,
		information.Size(),
		information.ModTime().UnixNano(),
	)
}

// hasMatchingMemorySnapshot reports whether CPU, memory, and disk match the warm
// image shape exactly.
func (runtime *Runtime) hasMatchingMemorySnapshot(specification vm.Specification) bool {
	configuration := specification.Image.MemorySnapshotConfiguration
	return !specification.Rescue.Enabled && specification.Image.CacheImage &&
		specification.Image.MemorySnapshot &&
		configuration != nil &&
		configuration.VirtualCPUCount == specification.VirtualCPUCount() &&
		configuration.MemoryMiB == specification.MemoryMiB &&
		configuration.DiskMiB == specification.DiskMiB
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
