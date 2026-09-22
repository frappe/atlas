package vm

import (
	"context"
	"errors"
	"maps"
	"slices"
)

// RequestRestart stores durable restart intent.
func (manager *Manager) RequestRestart(ctx context.Context, identifier string) error {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return err
	}
	defer unlock()
	if err := manager.assertNotSourceLocked(identifier); err != nil {
		return err
	}
	record, err := manager.store.readDesired(identifier)
	if err != nil {
		return err
	}
	if record.State != StateRunning {
		return ErrConflict
	}
	record.RestartGeneration++
	return manager.store.writeDesired(record)
}

// SetCompute stores the complete requested compute configuration.
func (manager *Manager) SetCompute(ctx context.Context, identifier string, compute Compute) error {
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if record.State == StateDestroyed {
			return false, ErrConflict
		}

		shapeChanged := record.Specification.CPUMillicores != compute.CPUMillicores ||
			record.Specification.MemoryMiB != compute.MemoryMiB
		timeoutChanged := record.Specification.SleepAfterIdleSeconds != compute.SleepAfterIdleSeconds
		if !shapeChanged && !timeoutChanged && record.State == StateRunning {
			return false, nil
		}

		if shapeChanged {
			observed, err := manager.store.readObserved(identifier)
			if err != nil {
				return false, err
			}
			if observed.State != StateStopped {
				return false, ErrConflict
			}
			record.Specification.CPUMillicores = compute.CPUMillicores
			record.Specification.MemoryMiB = compute.MemoryMiB
			record.SpecificationGeneration++
			record.State = StateRunning
		}

		record.Specification.SleepAfterIdleSeconds = compute.SleepAfterIdleSeconds
		return true, nil
	})
}

// SetDisk stores the complete requested disk configuration.
func (manager *Manager) SetDisk(ctx context.Context, identifier string, diskMiB int, limits Disk) error {
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if diskMiB < record.Specification.DiskMiB {
			return false, ErrConflict
		}
		if diskMiB == record.Specification.DiskMiB && limits == record.Specification.Disk {
			return false, nil
		}
		record.Specification.DiskMiB = diskMiB
		record.Specification.Disk = limits
		record.SpecificationGeneration++
		return true, nil
	})
}

// Resize stores the complete compute and disk shape in one desired-record write.
func (manager *Manager) Resize(ctx context.Context, identifier string, compute Compute, diskMiB int) error {
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if record.State == StateDestroyed || diskMiB < record.Specification.DiskMiB {
			return false, ErrConflict
		}

		shapeChanged := record.Specification.CPUMillicores != compute.CPUMillicores ||
			record.Specification.MemoryMiB != compute.MemoryMiB ||
			record.Specification.DiskMiB != diskMiB
		if shapeChanged {
			observed, err := manager.store.readObserved(identifier)
			if err != nil {
				return false, err
			}
			if observed.State != StateStopped {
				return false, ErrConflict
			}
		}

		timeoutChanged := record.Specification.SleepAfterIdleSeconds != compute.SleepAfterIdleSeconds
		if !shapeChanged && !timeoutChanged {
			return false, nil
		}

		record.Specification.CPUMillicores = compute.CPUMillicores
		record.Specification.MemoryMiB = compute.MemoryMiB
		record.Specification.DiskMiB = diskMiB
		record.Specification.SleepAfterIdleSeconds = compute.SleepAfterIdleSeconds
		if shapeChanged {
			record.SpecificationGeneration++
			record.State = StateRunning
		}
		return true, nil
	})
}

// LockCapacity serializes capacity checks with VM resource changes.
func (manager *Manager) LockCapacity() func() {
	manager.allocationMutex.Lock()
	return manager.allocationMutex.Unlock
}

// SetNetwork stores the complete requested network configuration.
func (manager *Manager) SetNetwork(ctx context.Context, identifier string, configuration NetworkConfiguration) error {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return err
	}
	defer unlock()
	if err := manager.assertNotSourceLocked(identifier); err != nil {
		return err
	}
	manager.allocationMutex.Lock()
	defer manager.allocationMutex.Unlock()
	record, err := manager.store.readDesired(identifier)
	if err != nil {
		return err
	}
	if configuration.Equal(record.Specification.Network) {
		return nil
	}
	if inUse, err := manager.publicIPv4InUse(identifier, configuration.PublicIPv4); err != nil {
		return err
	} else if inUse {
		return ErrConflict
	}
	record.Specification.Network = configuration
	record.SpecificationGeneration++
	record.Generation++
	return manager.store.writeDesired(record)
}

// ReplaceSSHKeys stores the complete desired SSH key list.
func (manager *Manager) ReplaceSSHKeys(ctx context.Context, identifier string, sshKeys []string) (bool, error) {
	_, err := manager.mutateAndReport(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if slices.Equal(record.Specification.SSHKeys, sshKeys) {
			return false, nil
		}
		record.Specification.SSHKeys = slices.Clone(sshKeys)
		return true, nil
	})
	if err != nil {
		return false, err
	}
	return manager.applyMetadata(ctx, identifier)
}

// ReplaceMetadata stores the complete desired guest metadata map.
func (manager *Manager) ReplaceMetadata(ctx context.Context, identifier string, metadata map[string]string) (bool, error) {
	_, err := manager.mutateAndReport(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if maps.Equal(record.Specification.Metadata, metadata) {
			return false, nil
		}
		record.Specification.Metadata = maps.Clone(metadata)
		return true, nil
	})
	if err != nil {
		return false, err
	}
	return manager.applyMetadata(ctx, identifier)
}

// Delete stores desired destruction.
func (manager *Manager) Delete(ctx context.Context, identifier string) error {
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if record.State == StateDestroyed {
			return false, nil
		}
		record.State = StateDestroyed
		return true, nil
	})
}

// ConnectSSH opens one guest SSH session.
func (manager *Manager) ConnectSSH(ctx context.Context, identifier string) (SSHConnection, error) {
	virtualMachine := manager.newVirtualMachine(identifier)
	unlock, err := virtualMachine.lock(ctx)
	if err != nil {
		return nil, err
	}
	defer unlock()
	desired, _, err := virtualMachine.records()
	if err != nil {
		return nil, err
	}
	networkInterface, err := manager.network.Ensure(ctx, networkRequest(desired))
	if err != nil {
		return nil, err
	}
	return manager.runtime.ConnectSSH(ctx, runtimeMachine(desired, networkInterface))
}

// CreateSnapshot stages one machine image snapshot.
func (manager *Manager) CreateSnapshot(ctx context.Context, identifier string) (StagedSnapshot, error) {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return StagedSnapshot{}, err
	}
	defer unlock()
	if err := manager.assertNotSourceLocked(identifier); err != nil {
		return StagedSnapshot{}, err
	}
	desired, err := manager.store.readDesired(identifier)
	if err != nil {
		return StagedSnapshot{}, err
	}
	observed, err := manager.store.readObserved(identifier)
	if err != nil {
		return StagedSnapshot{}, err
	}
	operationID := newOperationID()
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.inspect(ctx, identifier, machine, &observed, operationID)
	if err != nil {
		return StagedSnapshot{}, err
	}
	if status.State != StateRunning && status.State != StatePaused && status.State != StateStopped {
		observed.State = status.State
		observed.completeOperation()
		if err := manager.store.writeObserved(identifier, observed); err != nil {
			return StagedSnapshot{}, err
		}

		return StagedSnapshot{}, ErrConflict
	}
	var snapshot StagedSnapshot
	err = manager.runOperation(ctx, identifier, &observed, operationID, phaseSnapshot, func() (operationError error) {
		resume := status.State == StateRunning
		if resume {
			if err := manager.runtime.Pause(ctx, machine); err != nil {
				return err
			}
			defer func() {
				operationError = errors.Join(operationError, manager.runtime.Resume(context.WithoutCancel(ctx), machine))
			}()
		}
		snapshot, operationError = manager.snapshots.Stage(ctx, SnapshotRequest{
			VirtualMachineID: identifier,
			ImageReference:   desired.Specification.Image.Name,
		})
		return operationError
	})
	if err != nil {
		return StagedSnapshot{}, err
	}
	observed.State = status.State
	observed.completeOperation()
	if err := manager.store.writeObserved(identifier, observed); err != nil {
		return StagedSnapshot{}, err
	}

	return snapshot, nil
}

// applyMetadata pushes guest metadata without blocking the API for long.
func (manager *Manager) applyMetadata(ctx context.Context, identifier string) (bool, error) {
	operationContext, cancel := context.WithTimeout(ctx, manager.configuration.FastApplyTimeout)
	defer cancel()

	if err := manager.refreshMetadata(operationContext, identifier); err != nil {
		return false, nil
	}

	return true, nil
}

// refreshMetadata sends the stored specification to the running guest.
func (manager *Manager) refreshMetadata(ctx context.Context, identifier string) error {
	virtualMachine := manager.newVirtualMachine(identifier)
	unlock, err := virtualMachine.lock(ctx)
	if err != nil {
		return err
	}
	defer unlock()
	desired, observed, err := virtualMachine.records()
	if err != nil {
		return err
	}
	operationID := newOperationID()
	machine := runtimeMachine(desired, observed.NetworkInterface)
	if err := manager.runOperation(ctx, identifier, &observed, operationID, phaseMetadata, func() error {
		return manager.runtime.RefreshMetadata(ctx, machine)
	}); err != nil {
		return err
	}
	observed.completeOperation()

	return manager.store.writeObserved(identifier, observed)
}

// mutate applies one change to the desired record and discards the changed flag.
func (manager *Manager) mutate(ctx context.Context, identifier string, change func(*DesiredRecord) (bool, error)) error {
	_, err := manager.mutateAndReport(ctx, identifier, change)
	return err
}

// mutateAndReport applies one change and reports whether the record changed.
func (manager *Manager) mutateAndReport(ctx context.Context, identifier string, change func(*DesiredRecord) (bool, error)) (bool, error) {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return false, err
	}
	defer unlock()
	if err := manager.assertNotSourceLocked(identifier); err != nil {
		return false, err
	}

	record, err := manager.store.readDesired(identifier)
	if err != nil {
		return false, err
	}

	changed, err := change(&record)
	if err != nil {
		return false, err
	}
	if !changed {
		return false, nil
	}

	record.Generation++
	if err := manager.store.writeDesired(record); err != nil {
		return false, err
	}

	return true, nil
}
