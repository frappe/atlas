package vm

import "context"

// SetPowerState stores a power state.
func (manager *Manager) SetPowerState(ctx context.Context, identifier string, state State) error {
	switch state {
	case StateRunning, StateStopped, StatePaused:
	default:
		return ErrConflict
	}
	if state == StateRunning {
		desired, observed, err := manager.newVirtualMachine(identifier).records()
		if err != nil {
			return err
		}
		// A repeated start must not wait for a download that already owns the VM lock.
		if desired.State == state && observed.SleepState != SleepStateAwake && desired.Generation > observed.Generation {
			return manager.assertNotSourceLocked(identifier)
		}
	}

	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if state == StatePaused {
			if err := manager.assertNotInRescueMode(identifier); err != nil {
				return false, err
			}
		}
		return manager.changePowerState(record, state)
	})
}

func (manager *Manager) changePowerState(record *DesiredRecord, state State) (bool, error) {
	if record.State != state {
		record.State = state
		return true, nil
	}
	if state != StateRunning {
		return false, nil
	}
	observed, err := manager.store.readObserved(record.ID)
	return observed.SleepState != SleepStateAwake && record.Generation == observed.Generation, err
}
