package vm

import "context"

// SetPowerState stores a power state.
func (manager *Manager) SetPowerState(ctx context.Context, identifier string, state State) error {
	if state != StateRunning && state != StateStopped && state != StatePaused {
		return ErrConflict
	}
	err := manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if record.State == state {
			return false, nil
		}
		record.State = state
		return true, nil
	})
	if err != nil || state != StateRunning {
		return err
	}
	return manager.wakeSleepingVirtualMachine(ctx, identifier)
}
