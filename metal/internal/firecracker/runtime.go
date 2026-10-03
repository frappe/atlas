// Package firecracker controls Firecracker virtual machines.
package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// maxConcurrentSSHSessions limits host-wide SSH console sessions.
const maxConcurrentSSHSessions = 32

// virtualMachineStorage prepares and releases the disk of one VM.
type virtualMachineStorage interface {
	PrepareRescueBoot(context.Context, storage.VirtualMachineRescueBootRequest) (storage.BootConfiguration, error)
	PrepareBoot(ctx context.Context, request storage.VirtualMachineStorageRequest) (storage.BootConfiguration, error)
	PrepareRootFileSystem(ctx context.Context, request storage.VirtualMachineStorageRequest) error
	HasDisk(ctx context.Context, virtualMachineID string) (bool, error)
	Release(ctx context.Context, virtualMachineID string) error
}

// imageStore supplies boot images and their warm artifacts.
type imageStore interface {
	EnsureImage(ctx context.Context, image vm.Image) error
	WarmImage(
		ctx context.Context,
		image vm.Image,
		configuration vm.MemorySnapshotConfiguration,
		firecrackerCompatibility string,
	) (storage.WarmImageArtifacts, bool, error)
	RecordImageUse(imageReference string, usedAt time.Time) error
}

// serialBroker manages each VM's serial console PTY.
type serialBroker interface {
	Open(id string) error
	Persist(id string) error
	Close(id string) error
}

// Runtime manages Firecracker virtual machines on one host.
type Runtime struct {
	rescueMutex           sync.Mutex
	rescueListeners       map[string]*rescueListener
	rescueClosed          bool
	configuration         Config
	units                 platform.UnitManager
	virtualMachineStorage virtualMachineStorage
	imageStore            imageStore
	serialBroker          serialBroker
	sshSlots              chan struct{}
	logger                *slog.Logger
}

var _ vm.Runtime = (*Runtime)(nil)

// NewRuntime returns a Firecracker runtime.
func NewRuntime(
	configuration Config,
	units platform.UnitManager,
	virtualMachineStorage virtualMachineStorage,
	imageStore imageStore,
	serialBroker serialBroker,
	logger *slog.Logger,
) *Runtime {
	if logger == nil {
		logger = slog.Default()
	}

	return &Runtime{
		configuration:         configuration,
		units:                 units,
		virtualMachineStorage: virtualMachineStorage,
		imageStore:            imageStore,
		serialBroker:          serialBroker,
		sshSlots:              make(chan struct{}, maxConcurrentSSHSessions),
		logger:                logger,
	}
}

// Inspect returns the current systemd and Firecracker state.
func (runtime *Runtime) Inspect(ctx context.Context, input vm.RuntimeMachine) (vm.RuntimeStatus, error) {
	state, err := runtime.newMachine(input).status(ctx)
	if err != nil {
		return vm.RuntimeStatus{}, fmt.Errorf("inspect Firecracker VM: %w", err)
	}

	_, savedStateError := runtime.configuration.loadSavedState(runtime.newMachine(input).savedStateRequirement())
	hasSavedState := savedStateError == nil
	if savedStateError != nil && !errors.Is(savedStateError, errSavedStateNotFound) {
		return vm.RuntimeStatus{}, fmt.Errorf("validate saved VM state: %w", savedStateError)
	}
	reboot, err := runtime.inspectRescueBoot(input, state)
	if err != nil {
		return vm.RuntimeStatus{}, err
	}
	return vm.RuntimeStatus{State: state, HasSavedState: hasSavedState, RescueRebootRequested: reboot}, nil
}

// Start launches a VM from a warm image or a cold boot.
func (runtime *Runtime) Start(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).Start(ctx)
}

// ColdStart boots the received disk without restoring source guest memory.
func (runtime *Runtime) ColdStart(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).coldBoot(ctx)
}

// Stop shuts down a VM and deletes its saved state.
func (runtime *Runtime) Stop(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).Stop(ctx)
}

// SaveAndStop saves guest memory and stops Firecracker.
func (runtime *Runtime) SaveAndStop(ctx context.Context, input vm.RuntimeMachine) error {
	if input.Specification.Rescue.Enabled {
		return vm.ErrConflict
	}

	return runtime.newMachine(input).saveAndStop(ctx)
}

// Restore starts a VM from its saved state.
func (runtime *Runtime) Restore(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).startFromSavedState(ctx, true)
}

// RestorePaused loads saved state without starting the virtual CPUs.
func (runtime *Runtime) RestorePaused(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).startFromSavedState(ctx, false)
}

// DeleteSavedState removes the saved state of one VM.
func (runtime *Runtime) DeleteSavedState(_ context.Context, input vm.RuntimeMachine) error {
	return runtime.removeSavedState(input.ID)
}

// Pause pauses a running Firecracker virtual machine.
func (runtime *Runtime) Pause(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).Pause(ctx)
}

// Resume resumes a paused Firecracker virtual machine.
func (runtime *Runtime) Resume(ctx context.Context, input vm.RuntimeMachine) error {
	return runtime.newMachine(input).Resume(ctx)
}

// Remove stops the process and removes runtime-owned files.
func (runtime *Runtime) Remove(ctx context.Context, input vm.RuntimeMachine) error {
	if err := runtime.clearRescueBoot(input.ID); err != nil {
		return err
	}

	if err := runtime.newMachine(input).cleanupSystemd(ctx); err != nil {
		return err
	}

	if err := os.Remove(runtime.configuration.socketPath(input.ID)); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove Firecracker socket: %w", err)
	}
	if err := os.Remove(runtime.configuration.jailerEnvironmentPath(input.ID)); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove jailer environment: %w", err)
	}
	if err := os.RemoveAll(filepath.Dir(runtime.configuration.chrootRoot(input.ID))); err != nil {
		return fmt.Errorf("remove Firecracker jail: %w", err)
	}
	if err := runtime.removeSavedState(input.ID); err != nil {
		return err
	}

	return nil
}

// RefreshDisk applies the machine's disk limits to a live guest. A caller may
// lower the limit in the machine specification for a temporary throttle. A VM
// that is neither running nor paused is unchanged.
func (runtime *Runtime) RefreshDisk(ctx context.Context, input vm.RuntimeMachine) error {
	status, err := runtime.Inspect(ctx, input)
	if err != nil {
		return err
	}
	if status.State != vm.StateRunning && status.State != vm.StatePaused {
		return nil
	}

	paths := []string{rootDrivePath}
	if input.Specification.Rescue.Enabled {
		paths = append(paths, "/disk.img")
	}
	client := api.New(runtime.configuration.socketPath(input.ID))
	for index, path := range paths {
		if err := client.PatchDrive(ctx, api.PartialDrive{
			DriveID:     fmt.Sprintf("drive%d", index),
			PathOnHost:  path,
			RateLimiter: driveRateLimiter(input.Specification.Disk, len(paths)),
		}); err != nil {
			return err
		}
	}
	return nil
}
