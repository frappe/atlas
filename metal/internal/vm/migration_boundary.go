package vm

import "context"

// MigrationGuard reports whether a migration holds a VM.
type MigrationGuard interface {
	// IsSourceLocked reports whether a migration holds the VM as a source.
	IsSourceLocked(virtualMachineID string) bool
	// IsDestinationReserved reports whether a migration reserves this VM ID.
	IsDestinationReserved(virtualMachineID string) bool
}

// MigrationHost exposes the VM operations that the migration lifecycle owns.
type MigrationHost struct {
	manager *Manager
}

// NewMigrationHost returns the migration operations for manager.
func NewMigrationHost(manager *Manager) *MigrationHost {
	if manager == nil {
		return nil
	}
	return &MigrationHost{manager: manager}
}

// SetMigrationGuard records the guard that answers migration lock questions.
func (manager *Manager) SetMigrationGuard(guard MigrationGuard) {
	manager.migrationGuard = guard
}

// ReadDesired returns the desired record of one VM.
func (host *MigrationHost) ReadDesired(virtualMachineID string) (DesiredRecord, error) {
	return host.manager.store.readDesired(virtualMachineID)
}

// ReadObserved returns the observed record of one VM.
func (host *MigrationHost) ReadObserved(virtualMachineID string) (ObservedRecord, error) {
	return host.manager.store.readObserved(virtualMachineID)
}

// WriteDesired replaces the desired record of one VM.
func (host *MigrationHost) WriteDesired(record DesiredRecord) error {
	return host.manager.store.writeDesired(record)
}

// WriteObserved replaces the observed record of one VM.
func (host *MigrationHost) WriteObserved(virtualMachineID string, record ObservedRecord) error {
	return host.manager.store.writeObserved(virtualMachineID, record)
}

// RemoveVirtualMachineRecords removes the VM record directory, including migration state.
func (host *MigrationHost) RemoveVirtualMachineRecords(virtualMachineID string) error {
	host.manager.slidingWindowUsage.forget(virtualMachineID)
	return host.manager.store.remove(virtualMachineID)
}

// DesiredPath returns the desired record path of one VM.
func (host *MigrationHost) DesiredPath(virtualMachineID string) string {
	return host.manager.store.desiredPath(virtualMachineID)
}

// ObservedPath returns the observed record path of one VM.
func (host *MigrationHost) ObservedPath(virtualMachineID string) string {
	return host.manager.store.observedPath(virtualMachineID)
}

// AllocateUserID returns a free host user ID. The caller holds the allocation lock.
func (host *MigrationHost) AllocateUserID() (uint32, error) {
	return host.manager.allocateUserID()
}

// LockUserIDAllocation serializes user ID allocation and returns the unlock.
func (host *MigrationHost) LockUserIDAllocation() func() {
	host.manager.allocationMutex.Lock()
	return host.manager.allocationMutex.Unlock
}

// LockOperation takes the per-VM operation lock and returns the unlock.
func (host *MigrationHost) LockOperation(ctx context.Context, virtualMachineID string) (func(), error) {
	return host.manager.operationLocks.Lock(ctx, virtualMachineID)
}

// ReleaseStorage releases one VM's disk.
func (host *MigrationHost) ReleaseStorage(ctx context.Context, virtualMachineID string) error {
	return host.manager.storage.Release(ctx, virtualMachineID)
}

// VirtualMachineRecordsDirectory returns the root directory that holds VM and migration records.
func (host *MigrationHost) VirtualMachineRecordsDirectory() string {
	return host.manager.configuration.MachinesDirectory
}

// CloneSpecification copies a specification's reference fields for safe reuse.
func CloneSpecification(specification Specification) Specification {
	return cloneSpecification(specification)
}

// ValidIdentifier reports whether an identifier is safe to use as a directory name.
func ValidIdentifier(identifier string) bool {
	return validIdentifier(identifier)
}
