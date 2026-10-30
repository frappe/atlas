// Package firecracker controls Firecracker virtual machines.
package firecracker

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/firecracker/api"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/vm"
)

// maxConcurrentSSHSessions limits host-wide SSH console sessions.
const maxConcurrentSSHSessions = 32

// virtualMachineStorage prepares and releases the disk of one VM.
type virtualMachineStorage interface {
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

// serialBroker manages the console PTY of each VM and each SSH session.
type serialBroker interface {
	Open(id string) error
	Persist(id string) error
	Close(id string) error
	IsOpen(id string) bool
	Attach(ctx context.Context, id string, client io.ReadWriter, resize <-chan console.Winsize) error
}

// Runtime manages Firecracker virtual machines on one host.
type Runtime struct {
	configuration         Config
	units                 platform.UnitManager
	sshUnits              sshSessionUnits
	virtualMachineStorage virtualMachineStorage
	imageStore            imageStore
	serialBroker          serialBroker
	sshMutex              sync.Mutex
	sshSessions           map[string]*sshSession
	logger                *slog.Logger
	telemetry             *telemetry.Registry
}

var _ vm.Runtime = (*Runtime)(nil)

// NewRuntime returns a Firecracker runtime.
func NewRuntime(
	configuration Config,
	units platform.UnitManager,
	sshUnits sshSessionUnits,
	virtualMachineStorage virtualMachineStorage,
	imageStore imageStore,
	serialBroker serialBroker,
	logger *slog.Logger,
	registry *telemetry.Registry,
) *Runtime {
	configuration.CPUTemplate = bytes.Clone(configuration.CPUTemplate)
	if logger == nil {
		logger = slog.Default()
	}
	// A failure counter starts at zero, so its first failure reads as an increase.
	for _, kind := range []string{"cold", "warm", "restore"} {
		registry.Count("metal_vm_boot_unreachable_total", 0, telemetry.Labels{"kind": kind})
	}

	return &Runtime{
		configuration:         configuration,
		units:                 units,
		sshUnits:              sshUnits,
		virtualMachineStorage: virtualMachineStorage,
		imageStore:            imageStore,
		serialBroker:          serialBroker,
		sshSessions:           make(map[string]*sshSession),
		logger:                logger,
		telemetry:             registry,
	}
}

// countBoot counts one VM boot. A cold boot names why warm memory was not used
// when the image had it.
func (runtime *Runtime) countBoot(kind, fallbackReason string) {
	runtime.telemetry.Count("metal_vm_boots_total", 1, telemetry.Labels{"kind": kind, "fallback_reason": fallbackReason})
}

// CollectTelemetry reports the open guest SSH sessions.
func (runtime *Runtime) CollectTelemetry(context.Context) ([]telemetry.Sample, error) {
	runtime.sshMutex.Lock()
	defer runtime.sshMutex.Unlock()
	return []telemetry.Sample{{Metric: "metal_ssh_sessions", Value: float64(len(runtime.sshSessions))}}, nil
}

// Inspect returns the current systemd and Firecracker state.
func (runtime *Runtime) Inspect(ctx context.Context, input vm.RuntimeMachine) (vm.RuntimeStatus, error) {
	state, err := runtime.newMachine(input).status(ctx)
	if err != nil {
		return vm.RuntimeStatus{}, fmt.Errorf("inspect Firecracker VM: %w", err)
	}

	hasSavedState, err := runtime.hasSavedState(input)
	if err != nil {
		return vm.RuntimeStatus{}, err
	}
	return vm.RuntimeStatus{State: state, HasSavedState: hasSavedState}, nil
}

// hasSavedState reports whether the VM has a state it can restore. An incompatible state is
// removed, because a cold boot changes the disk and the old memory must not resume over it.
func (runtime *Runtime) hasSavedState(input vm.RuntimeMachine) (bool, error) {
	_, err := runtime.configuration.loadSavedState(runtime.newMachine(input).savedStateRequirement())
	switch {
	case err == nil:
		return true, nil
	case errors.Is(err, errSavedStateNotFound):
		return false, nil
	case errors.Is(err, errSavedStateIncompatible):
		return false, runtime.removeSavedState(input.ID)
	}
	return false, fmt.Errorf("validate saved VM state: %w", err)
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
	limiter := driveRateLimiter(input.Specification.Disk)
	if limiter == nil {
		limiter = &api.RateLimiter{}
	}
	// PATCH retains omitted buckets, so unlimited values must explicitly clear them.
	if limiter.Bandwidth == nil {
		limiter.Bandwidth = &api.TokenBucket{}
	}
	if limiter.Ops == nil {
		limiter.Ops = &api.TokenBucket{}
	}

	drive := api.PartialDrive{DriveID: rootDriveIdentifier, PathOnHost: rootDrivePath, RateLimiter: limiter}
	if runtime.IsInRescueMode(input.ID) {
		drive.DriveID, drive.PathOnHost = rescuedDriveIdentifier, rescuedDrivePath
	}
	return api.New(runtime.configuration.socketPath(input.ID)).PatchDrive(ctx, drive)
}

// IsInRescueMode reports whether the jail holds the rescued drive. A stop keeps
// the jail, and a cold boot or memory restore replaces it.
func (runtime *Runtime) IsInRescueMode(identifier string) bool {
	_, err := os.Stat(filepath.Join(runtime.configuration.chrootRoot(identifier), rescuedDrivePath))
	return err == nil
}

// GetUsage reads CPU and disk use from the unit cgroup and memory from the
// guest balloon report. Without a valid report, memory is from the cgroup.
func (runtime *Runtime) GetUsage(ctx context.Context, input vm.RuntimeMachine) (vm.RuntimeUsage, error) {
	diskPath := filepath.Join(runtime.configuration.chrootRoot(input.ID), rootDrivePath)
	diskInfo, err := os.Stat(diskPath)
	if err != nil {
		return vm.RuntimeUsage{}, fmt.Errorf("stat VM root disk %s: %w", diskPath, err)
	}
	if diskInfo.Mode()&os.ModeDevice == 0 || diskInfo.Mode()&os.ModeCharDevice != 0 {
		return vm.RuntimeUsage{}, fmt.Errorf("VM root disk %s is not a block device", diskPath)
	}
	deviceNumber := diskInfo.Sys().(*syscall.Stat_t).Rdev
	diskDevice := fmt.Sprintf("%d:%d", unix.Major(deviceNumber), unix.Minor(deviceNumber))
	usage, err := runtime.units.GetUsage(ctx, input.ID, diskDevice)
	if err != nil {
		return vm.RuntimeUsage{}, err
	}
	memoryBytes := usage.MemoryBytes
	statistics, err := api.New(runtime.configuration.socketPath(input.ID)).GetBalloonStatistics(ctx)
	if guestMemoryBytes, valid := statistics.UsedMemoryBytes(); err == nil && valid {
		memoryBytes = guestMemoryBytes
	}

	return vm.RuntimeUsage{
		MemoryBytes: memoryBytes, CPUTimeMicroseconds: usage.CPUTimeMicroseconds,
		DiskReadBytes: usage.DiskReadBytes, DiskWriteBytes: usage.DiskWriteBytes,
		DiskReadOperations: usage.DiskReadOperations, DiskWriteOperations: usage.DiskWriteOperations,
	}, nil
}
