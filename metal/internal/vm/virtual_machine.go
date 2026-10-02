package vm

import "context"

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

// records reads both stored records, so a caller sees one consistent pair.
func (virtualMachine virtualMachine) records() (DesiredRecord, ObservedRecord, error) {
	desired, err := virtualMachine.manager.store.readDesired(virtualMachine.identifier)
	if err != nil {
		return DesiredRecord{}, ObservedRecord{}, err
	}

	observed, err := virtualMachine.manager.store.readObserved(virtualMachine.identifier)
	if err != nil {
		return DesiredRecord{}, ObservedRecord{}, err
	}

	return desired, observed, nil
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
