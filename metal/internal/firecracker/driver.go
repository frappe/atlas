// Package firecracker controls Firecracker virtual machines.
package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log"
	"maps"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/google/uuid"
)

type virtualMachineStorage interface {
	PrepareBoot(ctx context.Context, request storage.VirtualMachineStorageRequest) (storage.BootConfiguration, error)
	PrepareRootFileSystem(ctx context.Context, request storage.VirtualMachineStorageRequest) error
	Release(ctx context.Context, virtualMachineID string) error
	ResizeDisk(ctx context.Context, virtualMachineID string, diskMiB int) error
	DiskUsage(ctx context.Context, virtualMachineID string) (storage.Usage, error)
}

type snapshotStore interface {
	StageSnapshot(ctx context.Context, virtualMachineID, snapshotID, imageReference string) (storage.StagedSnapshot, error)
}

type imageStore interface {
	EnsureImage(ctx context.Context, image vm.ImageRef) error
	WarmImage(
		ctx context.Context,
		image vm.ImageRef,
		configuration vm.MemorySnapshotConfiguration,
		firecrackerCompatibility string,
	) (storage.WarmImageArtifacts, bool, error)
	CreateWarmSourceSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error
	DeleteWarmSourceSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error
	PromoteWarmSnapshot(ctx context.Context, promotion storage.WarmImagePromotion) (storage.WarmImageArtifacts, error)
	RemoveOtherWarmImages(ctx context.Context, imageReference, desiredKey string) error
	RecordImageUse(imageReference string, usedAt time.Time) error
}

// consoleBroker manages each VM's serial console PTY.
type consoleBroker interface {
	Open(id string) error
	Close(id string) error
}

// Driver manages Firecracker virtual machines on one host.
type Driver struct {
	cfg                   Config
	units                 systemd.Manager
	virtualMachineStorage virtualMachineStorage
	imageStore            imageStore
	snapshotStore         snapshotStore
	networkAllocator      network.Allocator
	consoleBroker         consoleBroker
	allocationMutex       sync.Mutex
	operationLocks        operationLocks
	warmupDelay           time.Duration
	sshSlots              chan struct{}
}

// maxConcurrentSSHSessions limits host-wide SSH console sessions.
const maxConcurrentSSHSessions = 32

// New returns a Firecracker driver.
func New(
	configuration Config,
	units systemd.Manager,
	virtualMachineStorage virtualMachineStorage,
	imageStore imageStore,
	snapshotStore snapshotStore,
	networkAllocator network.Allocator,
	consoleBroker consoleBroker,
) *Driver {
	return &Driver{
		cfg:                   configuration,
		units:                 units,
		virtualMachineStorage: virtualMachineStorage,
		imageStore:            imageStore,
		snapshotStore:         snapshotStore,
		networkAllocator:      networkAllocator,
		consoleBroker:         consoleBroker,
		warmupDelay:           5 * time.Minute,
		sshSlots:              make(chan struct{}, maxConcurrentSSHSessions),
	}
}

// Create reserves a virtual machine in the running state.
func (d *Driver) Create(ctx context.Context, id string, spec vm.Spec) (_ vm.VM, err error) {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return nil, err
	}
	defer unlock()

	if id == "" || id == "." || filepath.Base(id) != id {
		return nil, vm.ErrConflict
	}
	existing, loadError := d.cfg.readVMConfig(id)
	if loadError == nil {
		if existing.DesiredState == vm.StateDestroyed || !existing.Spec.SameReservation(spec) {
			return nil, vm.ErrConflict
		}
		existing.Spec = existing.Spec.RefreshImageSource(spec)
		if err := d.cfg.writeVMConfig(existing); err != nil {
			return nil, err
		}
		return d.newMachine(existing), nil
	}
	if !errors.Is(loadError, vm.ErrNotFound) {
		return nil, loadError
	}

	d.allocationMutex.Lock()
	defer d.allocationMutex.Unlock()

	inUse, err := d.isPublicIPv4InUse(id, spec.Network.PublicIPv4)
	if err != nil {
		return nil, err
	}
	if inUse {
		return nil, vm.ErrConflict
	}

	userID, err := d.allocateUserID()
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			d.cleanup(context.WithoutCancel(ctx), id)
		}
	}()
	configuration := vmConfig{
		ID:           id,
		UID:          userID,
		GID:          userID,
		Sock:         d.cfg.sockPath(id),
		DesiredState: vm.StateRunning,
		Spec:         spec,
	}

	networkInterface := d.networkAllocator.Resolve(id)
	configuration.IP = networkInterface.GuestIPAddress
	configuration.MAC = networkInterface.MACAddress

	if err = d.cfg.writeVMConfig(configuration); err != nil {
		return nil, err
	}
	return d.newMachine(configuration), nil
}

// Load returns a virtual machine reservation.
func (d *Driver) Load(ctx context.Context, id string) (vm.VM, error) {
	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return nil, err
	}
	return d.newMachine(configuration), nil
}

// List returns all virtual machine reservations.
func (d *Driver) List(ctx context.Context) ([]vm.VM, error) {
	ids, err := d.cfg.listVMIDs()
	if err != nil {
		return nil, err
	}

	virtualMachines := make([]vm.VM, 0, len(ids))
	for _, id := range ids {
		configuration, err := d.cfg.readVMConfig(id)
		if errors.Is(err, vm.ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("load VM %s: %w", id, err)
		}
		virtualMachines = append(virtualMachines, d.newMachine(configuration))
	}
	return virtualMachines, nil
}

// IDs returns all reserved virtual machine IDs.
func (d *Driver) IDs(ctx context.Context) ([]string, error) {
	return d.cfg.listVMIDs()
}

// SetDesiredState records the requested state.
func (d *Driver) SetDesiredState(ctx context.Context, id string, state vm.State) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	if !vm.IsDesiredState(state) {
		return vm.ErrConflict
	}
	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	configuration.DesiredState = state
	return d.cfg.writeVMConfig(configuration)
}

// Reboot starts an asynchronous restart without changing desired state.
func (d *Driver) Reboot(_ context.Context, id string) error {
	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	if configuration.DesiredState != vm.StateRunning {
		return vm.ErrConflict
	}

	go d.reboot(id)
	return nil
}

// reboot stops and starts the guest.
func (d *Driver) reboot(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		log.Printf("firecracker: reboot vm %s: lock: %v", id, err)
		return
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil || configuration.DesiredState != vm.StateRunning {
		return
	}
	machine := d.newMachine(configuration)
	if err := machine.stopUnlocked(ctx); err != nil {
		log.Printf("firecracker: reboot vm %s: stop: %v", id, err)
		return
	}
	if err := machine.startUnlocked(ctx); err != nil {
		log.Printf("firecracker: reboot vm %s: start: %v", id, err)
	}
}

// ReplaceSSHKeys replaces the authorized SSH keys for one virtual machine.
func (d *Driver) ReplaceSSHKeys(ctx context.Context, id string, sshKeys []string) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	configuration.Spec.SSHKeys = append([]string(nil), sshKeys...)

	if err := d.updateRunningMetadata(ctx, id, configuration); err != nil {
		return err
	}
	return d.cfg.writeVMConfig(configuration)
}

// ReplaceMetadata replaces the custom metadata for one virtual machine.
func (d *Driver) ReplaceMetadata(ctx context.Context, id string, metadata map[string]string) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	configuration.Spec.Metadata = maps.Clone(metadata)

	if err := d.updateRunningMetadata(ctx, id, configuration); err != nil {
		return err
	}
	return d.cfg.writeVMConfig(configuration)
}

// UpdateNetwork updates mutable network settings without restarting the VM.
func (d *Driver) UpdateNetwork(ctx context.Context, id string, update vm.NetworkUpdate) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	if update.PublicIPv4 != "" {
		update.Egress = vm.EgressHost
	}
	if inUse, err := d.isPublicIPv4InUse(id, update.PublicIPv4); err != nil {
		return err
	} else if inUse {
		return vm.ErrConflict
	}

	desired := configuration.Spec.Network
	desired.Egress = update.Egress
	desired.PublicIPv4 = update.PublicIPv4
	desired.PrivateNetworkThroughputMbps = update.PrivateNetworkThroughputMbps
	desired.PublicNetworkThroughputMbps = update.PublicNetworkThroughputMbps

	request := network.UpdateRequest{
		VirtualMachineID: id,
		UserID:           configuration.UID,
		Previous:         configuration.Spec.Network,
		Desired:          desired,
	}
	if err := d.networkAllocator.Update(ctx, request); err != nil {
		return fmt.Errorf("update network: %w", err)
	}

	configuration.Spec.Network = desired
	if err := d.cfg.writeVMConfig(configuration); err != nil {
		rollbackRequest := request
		rollbackRequest.Previous, rollbackRequest.Desired = request.Desired, request.Previous
		return errors.Join(err, d.networkAllocator.Update(ctx, rollbackRequest))
	}
	return nil
}

// updateRunningMetadata updates MMDS before persisting the new VM spec
func (d *Driver) updateRunningMetadata(ctx context.Context, id string, configuration vmConfig) error {
	unitStatus, err := d.units.Status(ctx, id)
	if err != nil {
		return err
	}
	if unitStatus.ActiveState != "active" {
		return nil
	}
	return d.newMachine(configuration).api.PutMMDS(ctx, metadataServiceData(
		id, configuration.IP, configuration.MAC, configuration.Spec,
	))
}

// ResizeCompute changes a stopped virtual machine.
func (d *Driver) ResizeCompute(ctx context.Context, id string, virtualCPUCount, memoryMiB int) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	machine := d.newMachine(configuration)
	unitStatus, err := d.units.Status(ctx, id)
	if err != nil {
		return err
	}
	if machine.state(ctx, unitStatus) != vm.StateStopped {
		return vm.ErrConflict
	}

	configuration.Spec.VCPUs = virtualCPUCount
	configuration.Spec.MemoryMiB = memoryMiB
	configuration.DesiredState = vm.StateRunning
	return d.cfg.writeVMConfig(configuration)
}

// CreateSnapshot creates image staging with a new UUID.
func (d *Driver) CreateSnapshot(ctx context.Context, virtualMachineID string) (storage.StagedSnapshot, error) {
	snapshotID, err := uuid.NewV7()
	if err != nil {
		return storage.StagedSnapshot{}, fmt.Errorf("create snapshot ID: %w", err)
	}

	unlock, err := d.operationLocks.lock(ctx, virtualMachineID)
	if err != nil {
		return storage.StagedSnapshot{}, err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(virtualMachineID)
	if err != nil {
		return storage.StagedSnapshot{}, err
	}
	machine := d.newMachine(configuration)
	state, err := machine.status(ctx)
	if err != nil {
		return storage.StagedSnapshot{}, err
	}
	if state != vm.StateRunning && state != vm.StatePaused && state != vm.StateStopped {
		return storage.StagedSnapshot{}, vm.ErrConflict
	}

	resume := state == vm.StateRunning
	if resume {
		if err := machine.api.Pause(ctx); err != nil {
			return storage.StagedSnapshot{}, err
		}
	}

	snapshot, snapshotError := d.snapshotStore.StageSnapshot(ctx, virtualMachineID, snapshotID.String(), configuration.Spec.Image.Name)
	if resume {
		resumeError := machine.api.Resume(context.WithoutCancel(ctx))
		return snapshot, errors.Join(snapshotError, resumeError)
	}
	return snapshot, snapshotError
}

// Reconcile moves one virtual machine toward its desired state.
func (d *Driver) Reconcile(ctx context.Context, id string) error {
	unlock, err := d.operationLocks.lock(ctx, id)
	if err != nil {
		return err
	}
	defer unlock()

	configuration, err := d.cfg.readVMConfig(id)
	if err != nil {
		return err
	}
	if configuration.DesiredState == vm.StateDestroyed {
		return d.newMachine(configuration).destroyUnlocked(ctx)
	}

	machine := d.newMachine(configuration)
	unitStatus, err := d.units.Status(ctx, id)
	if err != nil {
		return err
	}
	observedState := machine.state(ctx, unitStatus)
	reconcileError := advance(ctx, machine, configuration.DesiredState, observedState)
	statusError := d.cfg.writeStatus(id, observedState, reconcileError)
	return errors.Join(reconcileError, statusError)
}

func (d *Driver) prepareBoot(ctx context.Context, configuration vmConfig, networkInterface network.Interface) error {
	if err := d.cfg.writeJailerEnv(
		configuration.ID,
		d.cfg.jailerArgs(
			configuration.ID,
			configuration.UID,
			configuration.GID,
			networkInterface.NetworkNamespacePath,
		),
	); err != nil {
		return err
	}
	if err := d.cfg.linkSocket(configuration.ID); err != nil {
		return err
	}
	// Open the PTY before systemd starts the unit.
	if err := d.consoleBroker.Open(configuration.ID); err != nil {
		return err
	}
	if err := d.units.Start(ctx, configuration.ID); err != nil {
		return err
	}
	if err := d.units.SetLimits(ctx, configuration.ID, resourceLimits(configuration.Spec)); err != nil {
		return err
	}
	if err := waitSocket(ctx, configuration.Sock); err != nil {
		return err
	}

	bootConfiguration, err := d.virtualMachineStorage.PrepareBoot(ctx, storage.VirtualMachineStorageRequest{
		VirtualMachineID: configuration.ID,
		ImageReference:   configuration.Spec.Image.Name,
		Image:            configuration.Spec.Image,
		ChrootRoot:       d.cfg.chrootRoot(configuration.ID),
		UserID:           configuration.UID,
		GroupID:          configuration.GID,
		DiskMiB:          configuration.Spec.DiskMiB,
	})
	if err != nil {
		return err
	}
	log.Printf(
		"firecracker: vm %s kernel=%s cmdline=%q",
		configuration.ID,
		bootConfiguration.Kernel,
		bootArguments(bootConfiguration, networkInterface),
	)
	return configure(
		ctx,
		api.New(configuration.Sock),
		configuration.ID,
		configuration.Spec,
		bootConfiguration,
		networkInterface,
	)
}

func (d *Driver) relaunch(ctx context.Context, configuration vmConfig) error {
	_ = d.units.Stop(ctx, configuration.ID)
	_ = os.RemoveAll(filepath.Dir(d.cfg.chrootRoot(configuration.ID)))

	networkInterface, err := d.allocateNetwork(ctx, configuration)
	if err != nil {
		return err
	}
	return d.prepareBoot(ctx, configuration, networkInterface)
}

func (d *Driver) launchSnapshot(
	ctx context.Context,
	configuration vmConfig,
	rootSnapshot string,
	stateFile string,
	memoryFile string,
	metadata map[string]any,
) error {
	_ = d.units.Stop(ctx, configuration.ID)
	_ = os.RemoveAll(filepath.Dir(d.cfg.chrootRoot(configuration.ID)))

	networkInterface, err := d.allocateNetwork(ctx, configuration)
	if err != nil {
		return err
	}

	if err := d.cfg.writeJailerEnv(
		configuration.ID,
		d.cfg.jailerArgs(
			configuration.ID,
			configuration.UID,
			configuration.GID,
			networkInterface.NetworkNamespacePath,
		),
	); err != nil {
		return err
	}
	if err := d.cfg.linkSocket(configuration.ID); err != nil {
		return err
	}
	// Open the PTY before systemd starts the unit.
	if err := d.consoleBroker.Open(configuration.ID); err != nil {
		return err
	}
	if err := d.units.Start(ctx, configuration.ID); err != nil {
		return err
	}
	if err := d.units.SetLimits(ctx, configuration.ID, resourceLimits(configuration.Spec)); err != nil {
		return err
	}
	if err := waitSocket(ctx, configuration.Sock); err != nil {
		return err
	}

	if err := d.virtualMachineStorage.PrepareRootFileSystem(ctx, storage.VirtualMachineStorageRequest{
		VirtualMachineID: configuration.ID,
		ImageReference:   configuration.Spec.Image.Name,
		Image:            configuration.Spec.Image,
		ChrootRoot:       d.cfg.chrootRoot(configuration.ID),
		UserID:           configuration.UID,
		GroupID:          configuration.GID,
		DiskMiB:          configuration.Spec.DiskMiB,
		SourceSnapshot:   rootSnapshot,
	}); err != nil {
		return err
	}

	stage := filepath.Join(d.cfg.chrootRoot(configuration.ID), "snap")
	if err := mkdirChown(stage, configuration.UID, configuration.GID); err != nil {
		return err
	}
	if err := copyChown(
		ctx,
		stateFile,
		filepath.Join(stage, "state"),
		configuration.UID,
		configuration.GID,
	); err != nil {
		return err
	}
	if err := copyChown(
		ctx,
		memoryFile,
		filepath.Join(stage, "mem"),
		configuration.UID,
		configuration.GID,
	); err != nil {
		return err
	}

	client := api.New(configuration.Sock)
	if err := client.LoadSnapshot(ctx, api.LoadSnapshotRequest{
		SnapshotPath: "snap/state",
		Memory:       api.MemoryBackend{Path: "snap/mem", Type: "File"},
		Resume:       false,
	}); err != nil {
		return err
	}
	if metadata != nil {
		if err := client.PutMMDS(ctx, metadata); err != nil {
			log.Printf("firecracker: vm %s MMDS refresh: %v", configuration.ID, err)
		}
	}
	return client.Resume(ctx)
}

func (d *Driver) launchWarmImage(ctx context.Context, configuration vmConfig, ref string) error {
	memorySnapshotConfiguration := configuration.Spec.Image.MemorySnapshotConfiguration
	if memorySnapshotConfiguration == nil || configuration.Spec.Image.Name != ref {
		return vm.ErrNotFound
	}

	artifacts, found, err := d.imageStore.WarmImage(
		ctx,
		configuration.Spec.Image,
		*memorySnapshotConfiguration,
		d.firecrackerCompatibility(),
	)
	if err != nil {
		return err
	}
	if !found {
		return vm.ErrNotFound
	}

	metadata := metadataServiceData(
		configuration.ID, configuration.IP, configuration.MAC, configuration.Spec,
	)
	return d.launchSnapshot(
		ctx,
		configuration,
		artifacts.RootSnapshot,
		artifacts.StateFile,
		artifacts.MemoryFile,
		metadata,
	)
}

func (d *Driver) firecrackerCompatibility() string {
	information, err := os.Stat(d.cfg.FirecrackerBin)
	if err != nil {
		return d.cfg.FirecrackerBin
	}
	return fmt.Sprintf(
		"%s:%d:%d",
		d.cfg.FirecrackerBin,
		information.Size(),
		information.ModTime().UnixNano(),
	)
}

func (d *Driver) hasMatchingMemorySnapshot(spec vm.Spec) bool {
	configuration := spec.Image.MemorySnapshotConfiguration
	return spec.Image.CacheImage &&
		spec.Image.MemorySnapshot &&
		configuration != nil &&
		configuration.VirtualCPUCount == spec.VCPUs &&
		configuration.MemoryMiB == spec.MemoryMiB &&
		configuration.DiskMiB == spec.DiskMiB
}

func mkdirChown(path string, userID, groupID uint32) error {
	if err := os.MkdirAll(path, 0o750); err != nil {
		return err
	}
	return os.Chown(path, int(userID), int(groupID))
}

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

func (d *Driver) allocateNetwork(ctx context.Context, configuration vmConfig) (network.Interface, error) {
	return d.networkAllocator.Allocate(ctx, network.Request{
		VirtualMachineID:             configuration.ID,
		Egress:                       configuration.Spec.Network.Egress,
		PublicIPv4:                   configuration.Spec.Network.PublicIPv4,
		PrivateNetworkThroughputMbps: configuration.Spec.Network.PrivateNetworkThroughputMbps,
		PublicNetworkThroughputMbps:  configuration.Spec.Network.PublicNetworkThroughputMbps,
		UserID:                       configuration.UID,
		GroupID:                      configuration.GID,
	})
}

func (d *Driver) allocateUserID() (uint32, error) {
	usedIDs, err := d.cfg.usedIDs()
	if err != nil {
		return 0, err
	}
	return d.cfg.IDs.Allocate(usedIDs)
}

func (d *Driver) isPublicIPv4InUse(id, address string) (bool, error) {
	if address == "" {
		return false, nil
	}

	ids, err := d.cfg.listVMIDs()
	if err != nil {
		return false, err
	}
	for _, otherID := range ids {
		if otherID == id {
			continue
		}

		configuration, err := d.cfg.readVMConfig(otherID)
		if err != nil {
			return false, err
		}
		if configuration.Spec.Network.PublicIPv4 == address {
			return true, nil
		}
	}
	return false, nil
}

func (d *Driver) cleanup(ctx context.Context, id string) {
	_ = d.units.Stop(ctx, id)
	_ = d.networkAllocator.Release(ctx, id)
	_ = d.virtualMachineStorage.Release(ctx, id)
	_ = os.Remove(d.cfg.sockPath(id))
	_ = os.RemoveAll(d.cfg.vmDir(id))
}

var _ vm.Driver = (*Driver)(nil)
