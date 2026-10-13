package vm

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"
)

// RescueAccess selects how the original disk is exposed to the rescue guest.
type RescueAccess string

const (
	// RescueProtected requires a verified checkpoint before writable access.
	RescueProtected RescueAccess = "protected"
	// RescueReadOnly makes Firecracker refuse original-disk writes.
	RescueReadOnly RescueAccess = "read_only"
	// RescueUnprotected permits writes after explicit caller acknowledgement.
	RescueUnprotected RescueAccess = "unprotected"
)

func (access RescueAccess) valid() bool {
	return access == RescueProtected || access == RescueReadOnly || access == RescueUnprotected
}

// Recovery is the durable checkpoint lifecycle, owned under the VM operation lock.
// It lives independently of the rescue image and boot-selection generations.
type Recovery struct {
	Generation      uint64    `json:"generation"`
	GUID            string    `json:"guid,omitempty"`
	Active          bool      `json:"active"`
	ExpiresAt       time.Time `json:"expires_at,omitempty"`
	Action          string    `json:"action,omitempty"`
	RollbackApplied bool      `json:"rollback_applied"`
	Complete        bool      `json:"complete"`
}

func (recovery Recovery) validate() error {
	if recovery.Generation == 0 {
		if recovery != (Recovery{}) {
			return fmt.Errorf("recovery state has no session generation")
		}
		return nil
	}
	switch recovery.Action {
	case "", "keep", "rollback", "finish":
	default:
		return fmt.Errorf("invalid recovery disposition")
	}
	if recovery.RollbackApplied && recovery.Action != "rollback" {
		return fmt.Errorf("rollback completion has no rollback intent")
	}
	if recovery.Complete && recovery.Active {
		return fmt.Errorf("completed recovery remains active")
	}
	return recovery.validateCheckpointIdentity()
}

func (recovery Recovery) validateCheckpointIdentity() error {
	if recovery.GUID != "" {
		value, err := strconv.ParseUint(recovery.GUID, 10, 64)
		if err != nil || value == 0 {
			return fmt.Errorf("invalid recovery checkpoint GUID")
		}
	}
	return nil
}

// HasCheckpoint reports a recorded baseline which has not been resolved.
func (recovery Recovery) HasCheckpoint() bool { return recovery.GUID != "" && !recovery.Complete }

// Unresolved reports a session whose storage restrictions must remain in force.
func (recovery Recovery) Unresolved() bool { return recovery.Generation != 0 && !recovery.Complete }

// Expired reports whether the original deadline passed outside active rescue.
func (recovery Recovery) Expired(now time.Time) bool {
	return !recovery.Active && !recovery.ExpiresAt.IsZero() && !now.Before(recovery.ExpiresAt)
}

// RescueCheckpointSpace reports capacity outside the volume reservation.
type RescueCheckpointSpace struct {
	AvailableBytes uint64
	RequiredBytes  uint64
}

// RescueCheckpointSpaceError reports a failed capacity preflight.
type RescueCheckpointSpaceError struct{ Space RescueCheckpointSpace }

// Error describes the capacity shortfall without host paths.
func (err *RescueCheckpointSpaceError) Error() string {
	return fmt.Sprintf("rescue checkpoint requires %d available bytes; %d available", err.Space.RequiredBytes, err.Space.AvailableBytes)
}

// RequestRecoveryAction accepts rollback or finish for the current checkpoint.
// The same accepted action is idempotent. A competing disposition conflicts.
func (manager *Manager) RequestRecoveryAction(ctx context.Context, identifier, action string, generation uint64) error {
	switch action {
	case "rollback", "finish":
	default:
		return ErrConflict
	}
	return manager.mutate(ctx, identifier, func(record *DesiredRecord) (bool, error) {
		if generation == 0 || generation != record.Recovery.Generation {
			return false, ErrConflict
		}
		if record.Recovery.Action == action {
			return false, nil
		}
		if err := record.validateRecoveryAction(action); err != nil {
			return false, err
		}
		if action == "rollback" {
			if err := manager.verifyRecoveryCheckpoint(ctx, *record); err != nil {
				return false, err
			}
			record.Specification.Rescue = Rescue{}
			record.RescueGeneration++
			record.RestartGeneration++
		}
		record.Recovery.Action = action
		return true, nil
	})
}

func (record DesiredRecord) validateRecoveryAction(action string) error {
	if record.State != StateRunning && record.State != StateStopped {
		return ErrConflict
	}
	if record.Recovery.Action != "" || !record.Recovery.HasCheckpoint() {
		return ErrConflict
	}
	if action == "finish" && record.Recovery.Active {
		return ErrConflict
	}
	if record.Recovery.Expired(time.Now()) {
		return ErrConflict
	}
	return nil
}

func (manager *Manager) verifyRecoveryCheckpoint(ctx context.Context, desired DesiredRecord) error {
	guid, err := manager.storage.RescueCheckpointGUID(ctx, desired.ID, desired.Recovery.Generation)
	if err != nil {
		return err
	}
	if guid != desired.Recovery.GUID {
		return fmt.Errorf("recovery checkpoint identity changed: %w", ErrConflict)
	}
	return nil
}

// preflightRecovery runs before reconciliation can stop the original guest.
func (manager *Manager) preflightRecovery(ctx context.Context, desired DesiredRecord) error {
	if desired.Specification.Rescue.Enabled && desired.Specification.Rescue.Access == RescueProtected && desired.Recovery.GUID == "" {
		// Snapshot creation may have completed before its GUID was persisted.
		// Reusing that owned snapshot consumes no new checkpoint capacity.
		if _, err := manager.storage.RescueCheckpointGUID(ctx, desired.ID, desired.Recovery.Generation); err == nil {
			return nil
		} else if !errors.Is(err, ErrNotFound) {
			return err
		}
		_, err := manager.storage.CheckRescueCheckpointSpace(ctx, desired.ID)
		return err
	}
	return nil
}

// expireRecovery only discards a retained baseline. Active sessions and accepted
// rollback requests remain protected even after the original deadline.
func (manager *Manager) expireRecovery(ctx context.Context, desired *DesiredRecord) error {
	if !desired.Recovery.HasCheckpoint() {
		return nil
	}
	if desired.Recovery.Action == "" && desired.Recovery.Expired(time.Now()) {
		desired.Recovery.Action = "finish"
		if err := manager.store.writeDesired(*desired); err != nil {
			return err
		}
	}
	if desired.Recovery.Action != "finish" {
		return nil
	}
	return manager.discardRecovery(ctx, desired)
}

func (manager *Manager) discardRecovery(ctx context.Context, desired *DesiredRecord) error {
	if err := manager.storage.DiscardRescueCheckpoint(ctx, desired.ID, desired.Recovery.Generation, desired.Recovery.GUID); err != nil {
		return err
	}
	desired.Recovery.Complete = true
	desired.Recovery.Active = false
	return manager.store.writeDesired(*desired)
}

// applyRecoveryStopped runs only after all guest disk descriptors are closed.
func (manager *Manager) applyRecoveryStopped(ctx context.Context, desired *DesiredRecord) error {
	recovery := &desired.Recovery
	if !recovery.Unresolved() {
		return nil
	}
	if recovery.Action == "rollback" {
		return manager.applyRecoveryRollback(ctx, desired)
	}
	if desired.Specification.Rescue.Enabled {
		return manager.ensureRecoveryCheckpoint(ctx, desired)
	}
	return manager.retainRecoveryCheckpoint(ctx, desired)
}

func (manager *Manager) retainRecoveryCheckpoint(ctx context.Context, desired *DesiredRecord) error {
	recovery := &desired.Recovery
	if recovery.Active {
		// A crash after snapshot creation can leave its GUID unrecorded.
		// Adopt only the session-owned snapshot before resolving the exit.
		if recovery.GUID == "" {
			guid, err := manager.storage.RescueCheckpointGUID(ctx, desired.ID, recovery.Generation)
			if err != nil && !errors.Is(err, ErrNotFound) {
				return err
			}
			recovery.GUID = guid
		}
		recovery.Active = false
		recovery.Action = ""
		if recovery.GUID == "" {
			recovery.Complete = true
		} else if recovery.ExpiresAt.IsZero() {
			recovery.ExpiresAt = time.Now().UTC().Add(12 * time.Hour)
		}
		if err := manager.store.writeDesired(*desired); err != nil {
			return err
		}
	}
	return manager.expireRecovery(ctx, desired)
}

func (manager *Manager) applyRecoveryRollback(ctx context.Context, desired *DesiredRecord) error {
	recovery := &desired.Recovery
	if !recovery.RollbackApplied {
		if err := manager.storage.RollbackRescueCheckpoint(ctx, desired.ID, recovery.Generation, recovery.GUID); err != nil {
			return err
		}
		recovery.RollbackApplied = true
		if err := manager.store.writeDesired(*desired); err != nil {
			return err
		}
	}
	return manager.discardRecovery(ctx, desired)
}

func (manager *Manager) ensureRecoveryCheckpoint(ctx context.Context, desired *DesiredRecord) error {
	recovery := &desired.Recovery
	if desired.Specification.Rescue.Access != RescueProtected {
		return nil
	}
	if recovery.GUID == "" {
		guid, err := manager.storage.CreateRescueCheckpoint(ctx, desired.ID, recovery.Generation)
		if err != nil {
			return err
		}
		recovery.GUID = guid
		return manager.store.writeDesired(*desired)
	}
	return manager.verifyRecoveryCheckpoint(ctx, *desired)
}
