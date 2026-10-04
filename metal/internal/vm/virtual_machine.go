package vm

import (
	"context"
	"errors"
	"time"
)

// virtualMachine binds a manager to one identifier for one operation.
type virtualMachine struct {
	manager    *Manager
	identifier string
}

// newVirtualMachine returns a handle for one identifier.
func (manager *Manager) newVirtualMachine(identifier string) virtualMachine {
	return virtualMachine{manager: manager, identifier: identifier}
}

// lock serializes operations on this virtual machine.
func (virtualMachine virtualMachine) lock(ctx context.Context) (func(), error) {
	return virtualMachine.manager.operationLocks.Lock(ctx, virtualMachine.identifier)
}

// records reads both stored records, retrying briefly when removal leaves one
// temporarily absent, so a caller sees one consistent pair.
func (virtualMachine virtualMachine) records() (DesiredRecord, ObservedRecord, error) {
	var lastError error

	for attempt := range informationAttempts {
		if attempt > 0 {
			time.Sleep(informationRetryDelay)
		}

		desired, desiredError := virtualMachine.manager.store.readDesired(virtualMachine.identifier)
		observed, observedError := virtualMachine.manager.store.readObserved(virtualMachine.identifier)
		if desiredError == nil && observedError == nil {
			return desired, observed, nil
		}

		lastError = errors.Join(desiredError, observedError)
		if !isTornRemoval(desiredError, observedError) {
			return DesiredRecord{}, ObservedRecord{}, lastError
		}
	}

	return DesiredRecord{}, ObservedRecord{}, lastError
}

// runtimeMachine builds runtime input with a copied specification.
func runtimeMachine(record DesiredRecord, networkInterface NetworkInterface) RuntimeMachine {
	return RuntimeMachine{
		ID:                      record.ID,
		UserID:                  record.UserID,
		GroupID:                 record.GroupID,
		Specification:           cloneSpecification(record.Specification),
		NetworkInterface:        networkInterface,
		SpecificationGeneration: record.SpecificationGeneration,
		RestartGeneration:       record.RestartGeneration,
	}
}
