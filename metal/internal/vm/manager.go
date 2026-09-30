// Package vm owns virtual machine state and reconciliation.
package vm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
	"github.com/google/uuid"
)

// defaultFastApplyTimeout bounds an immediate guest update.
const defaultFastApplyTimeout = 2 * time.Second

const informationAttempts = 3

const informationRetryDelay = 5 * time.Millisecond

// ManagerConfig contains persistent VM manager settings.
type ManagerConfig struct {
	MachinesDirectory string
	UserIDRange       UserIDRange
	FastApplyTimeout  time.Duration
}

// ManagerDependencies contains the host services used by Manager.
type ManagerDependencies struct {
	Runtime   Runtime
	Network   Network
	Storage   Storage
	Snapshots Snapshots
	Traffic   *traffic.Monitor
	Logger    *slog.Logger
}

// Manager owns virtual machine desired state and reconciliation.
type Manager struct {
	configuration        ManagerConfig
	store                *recordStore
	runtime              Runtime
	network              Network
	storage              Storage
	snapshots            Snapshots
	traffic              *traffic.Monitor
	logger               *slog.Logger
	operationLocks       KeyedLocks
	allocationMutex      sync.Mutex
	temporaryUserIDs     map[uint32]bool
	temporaryIdentifiers map[string]bool
	migrationGuard       MigrationGuard
}

// NewManager validates all records and returns one host VM manager.
func NewManager(configuration ManagerConfig, dependencies ManagerDependencies) (*Manager, error) {
	if configuration.MachinesDirectory == "" {
		return nil, fmt.Errorf("VM machines directory is required")
	}
	if configuration.UserIDRange == (UserIDRange{}) {
		configuration.UserIDRange = DefaultUserIDRange
	}
	if configuration.FastApplyTimeout <= 0 {
		configuration.FastApplyTimeout = defaultFastApplyTimeout
	}
	if dependencies.Runtime == nil || dependencies.Network == nil || dependencies.Storage == nil || dependencies.Snapshots == nil {
		return nil, fmt.Errorf("VM manager dependencies are required")
	}
	if dependencies.Logger == nil {
		dependencies.Logger = slog.Default()
	}
	manager := &Manager{
		configuration:        configuration,
		store:                newRecordStore(configuration.MachinesDirectory),
		runtime:              dependencies.Runtime,
		network:              dependencies.Network,
		storage:              dependencies.Storage,
		snapshots:            dependencies.Snapshots,
		traffic:              dependencies.Traffic,
		logger:               dependencies.Logger,
		temporaryUserIDs:     make(map[uint32]bool),
		temporaryIdentifiers: make(map[string]bool),
	}
	if err := manager.store.validateAll(); err != nil {
		return nil, fmt.Errorf("validate VM records: %w", err)
	}
	return manager, nil
}

// Create reserves a VM or refreshes a matching retry. The fingerprint excludes
// rotating signed image URLs, so a retry does not reserve a second VM.
func (manager *Manager) Create(ctx context.Context, identifier string, specification Specification) (Information, error) {
	if !validIdentifier(identifier) {
		return Information{}, ErrConflict
	}
	if err := specification.validateMetadataServiceSize(identifier); err != nil {
		return Information{}, err
	}
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return Information{}, err
	}
	defer unlock()
	if err := manager.assertNotSourceLocked(identifier); err != nil {
		return Information{}, err
	}

	fingerprint, err := createFingerprint(specification)
	if err != nil {
		return Information{}, err
	}
	existing, err := manager.store.readDesired(identifier)
	if err == nil {
		if existing.State == StateDestroyed || existing.CreateFingerprint != fingerprint {
			return Information{}, ErrConflict
		}
		existing.Specification = existing.Specification.RefreshImageSource(specification)
		if err := manager.store.writeDesired(existing); err != nil {
			return Information{}, err
		}
		return manager.information(identifier)
	}
	if !errors.Is(err, ErrNotFound) {
		return Information{}, err
	}

	manager.allocationMutex.Lock()
	if manager.temporaryIdentifiers[identifier] {
		manager.allocationMutex.Unlock()
		return Information{}, ErrConflict
	}
	defer manager.allocationMutex.Unlock()
	if manager.isDestinationReserved(identifier) {
		return Information{}, ErrConflict
	}
	if inUse, err := manager.publicIPv4InUse(identifier, specification.Network.PublicIPv4); err != nil {
		return Information{}, err
	} else if inUse {
		return Information{}, ErrConflict
	}
	userID, err := manager.allocateUserID()
	if err != nil {
		return Information{}, err
	}
	desired := DesiredRecord{
		ID:                      identifier,
		UserID:                  userID,
		GroupID:                 userID,
		CreateFingerprint:       fingerprint,
		Generation:              1,
		SpecificationGeneration: 1,
		State:                   StateRunning,
		Specification:           cloneSpecification(specification),
	}
	observed := ObservedRecord{State: StateUnknown, UpdatedAt: time.Now().UTC()}
	if err := manager.store.writeDesired(desired); err != nil {
		return Information{}, err
	}
	if err := manager.store.writeObserved(identifier, observed); err != nil {
		return Information{}, errors.Join(err, manager.store.remove(identifier))
	}
	return informationFromRecords(desired, observed, DiskUsage{}), nil
}

// Information returns the persisted desired and observed VM state. It takes no
// operation lock, because a status read must not wait for the reconcile pass
// that holds the lock across an image pull and a boot. Each record is written
// atomically, so a read sees whole records.
func (manager *Manager) Information(_ context.Context, identifier string) (Information, error) {
	// Incoming migration destinations are not normal VMs.
	if manager.isDestinationReserved(identifier) {
		return Information{}, ErrNotFound
	}

	return manager.information(identifier)
}

// List returns all valid virtual machine records. A directory without both
// records is skipped, because a VM that is created or destroyed now must not
// fail the whole list.
func (manager *Manager) List(ctx context.Context) ([]Information, error) {
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return nil, err
	}
	information := make([]Information, 0, len(identifiers))
	for _, identifier := range identifiers {
		// Hide incoming migration destinations from the VM list.
		if manager.isDestinationReserved(identifier) {
			continue
		}
		current, err := manager.Information(ctx, identifier)
		if errors.Is(err, ErrNotFound) {
			manager.logger.WarnContext(ctx, "skipped incomplete virtual machine records",
				"virtual_machine_id", identifier, "error", err)
			continue
		}
		if err != nil {
			return nil, err
		}
		information = append(information, current)
	}
	return information, nil
}

// ListIDs returns all reserved virtual machine identifiers.
func (manager *Manager) ListIDs(_ context.Context) ([]string, error) {
	return manager.store.listIDs()
}

// information retries a read when removal leaves one record temporarily absent.
func (manager *Manager) information(identifier string) (Information, error) {
	var lastError error

	for attempt := range informationAttempts {
		if attempt > 0 {
			time.Sleep(informationRetryDelay)
		}

		desired, desiredError := manager.store.readDesired(identifier)
		observed, observedError := manager.store.readObserved(identifier)
		if desiredError == nil && observedError == nil {
			return informationFromRecords(desired, observed, observed.Disk), nil
		}

		lastError = errors.Join(desiredError, observedError)
		if !isTornRemoval(desiredError, observedError) {
			return Information{}, lastError
		}
	}

	return Information{}, lastError
}

// isTornRemoval reports whether removal left one record temporarily absent.
func isTornRemoval(desiredError, observedError error) bool {
	if errors.Is(desiredError, ErrNotFound) && observedError == nil {
		return true
	}

	return errors.Is(observedError, ErrNotFound) && desiredError == nil
}

// allocateUserID returns a host user ID that no stored or temporary VM holds.
func (manager *Manager) allocateUserID() (uint32, error) {
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return 0, err
	}
	used := make(map[uint32]bool, len(identifiers))
	for _, identifier := range identifiers {
		record, err := manager.store.readDesired(identifier)
		if err != nil {
			return 0, err
		}
		used[record.UserID] = true
	}
	for userID := range manager.temporaryUserIDs {
		used[userID] = true
	}
	return manager.configuration.UserIDRange.allocate(used)
}

// publicIPv4InUse reports whether another live VM already reserves address.
func (manager *Manager) publicIPv4InUse(identifier, address string) (bool, error) {
	if address == "" {
		return false, nil
	}
	identifiers, err := manager.store.listIDs()
	if err != nil {
		return false, err
	}
	for _, existingIdentifier := range identifiers {
		if existingIdentifier == identifier {
			continue
		}
		record, err := manager.store.readDesired(existingIdentifier)
		if err != nil {
			return false, err
		}
		if record.State != StateDestroyed && record.Specification.Network.PublicIPv4 == address {
			return true, nil
		}
	}
	return false, nil
}

// validIdentifier rejects an identifier that would escape the machines directory.
func validIdentifier(identifier string) bool {
	return identifier != "" && identifier != "." && filepath.Base(identifier) == identifier
}

// createFingerprint identifies the reservation requested by a create call.
func createFingerprint(specification Specification) (string, error) {
	normalized := cloneSpecification(specification)
	normalized.Image.RootfsURL = ""
	normalized.Image.KernelURL = ""
	data, err := json.Marshal(normalized)
	if err != nil {
		return "", fmt.Errorf("encode create fingerprint: %w", err)
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:]), nil
}

// cloneSpecification copies reference fields before runtime use.
func cloneSpecification(specification Specification) Specification {
	specification.SSHKeys = slices.Clone(specification.SSHKeys)
	specification.Metadata = maps.Clone(specification.Metadata)
	specification.Network.Firewall = specification.Network.Firewall.clone()
	if specification.Image.MemorySnapshotConfiguration != nil {
		configuration := *specification.Image.MemorySnapshotConfiguration
		specification.Image.MemorySnapshotConfiguration = &configuration
	}
	if specification.Rescue.Image.MemorySnapshotConfiguration != nil {
		configuration := *specification.Rescue.Image.MemorySnapshotConfiguration
		specification.Rescue.Image.MemorySnapshotConfiguration = &configuration
	}
	return specification
}

// informationFromRecords combines records into the public VM view and drops
// local error detail.
func informationFromRecords(desired DesiredRecord, observed ObservedRecord, usage DiskUsage) Information {
	var errorDetail *PublicOperationError
	if observed.Error != nil {
		errorDetail = &PublicOperationError{
			Code:      observed.Error.Code,
			Message:   observed.Error.Message,
			UpdatedAt: observed.Error.UpdatedAt,
		}
	}
	// A VM with no disk usage yet reports its requested size.
	if usage.SizeMiB == 0 {
		usage.SizeMiB = desired.Specification.DiskMiB
	}
	return Information{
		Rescue:                        cloneSpecification(desired.Specification).Rescue,
		ObservedRescueEnabled:         observed.RescueEnabled,
		DesiredRescueGeneration:       desired.RescueGeneration,
		ObservedRescueGeneration:      observed.RescueGeneration,
		ID:                            desired.ID,
		State:                         observed.State,
		DesiredState:                  desired.State,
		Error:                         errorDetail,
		CPUMillicores:                 desired.Specification.CPUMillicores,
		MemoryMiB:                     desired.Specification.MemoryMiB,
		DiskMiB:                       usage.SizeMiB,
		DiskUsedMiB:                   usage.UsedMiB,
		DiskThroughputMiBps:           desired.Specification.Disk.ThroughputMiBps,
		DiskIOPS:                      desired.Specification.Disk.IOPS,
		Image:                         desired.Specification.Image,
		SSHKeys:                       slices.Clone(desired.Specification.SSHKeys),
		Hostname:                      desired.Specification.Hostname,
		Metadata:                      maps.Clone(desired.Specification.Metadata),
		SleepAfterIdleSeconds:         desired.Specification.SleepAfterIdleSeconds,
		MAC:                           observed.NetworkInterface.MACAddress,
		PublicIPv4:                    desired.Specification.Network.PublicIPv4,
		Routes:                        slices.Clone(desired.Specification.Network.Routes),
		IsNetworkGateway:              desired.Specification.Network.IsNetworkGateway,
		PublicIPv6:                    desired.Specification.Network.PublicIPv6,
		WireGuardMeshIPv6:             desired.Specification.Network.WireGuardMeshIPv6,
		PrivateNetworkThroughputMiBps: desired.Specification.Network.PrivateNetworkThroughputMiBps,
		PublicNetworkThroughputMiBps:  desired.Specification.Network.PublicNetworkThroughputMiBps,
		Firewall:                      desired.Specification.Network.Firewall.clone(),
		DesiredGeneration:             desired.Generation,
		DesiredRestartGeneration:      desired.RestartGeneration,
		ObservedGeneration:            observed.Generation,
		ObservedRestartGeneration:     observed.RestartGeneration,
		Phase:                         observed.Phase,
		OperationID:                   observed.OperationID,
		OperationStartedAt:            observed.OperationStartedAt,
		UpdatedAt:                     observed.UpdatedAt,
	}
}

// newOperationID returns a sortable correlation ID for one reconcile pass.
func newOperationID() string {
	identifier, err := uuid.NewV7()
	if err != nil {
		return uuid.NewString()
	}
	return identifier.String()
}
