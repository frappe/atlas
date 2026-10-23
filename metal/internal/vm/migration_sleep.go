package vm

import "fmt"

// SleepMigration carries an offloaded checkpoint without loading guest memory.
type SleepMigration struct {
	State      SleepState      `json:"state"`
	Checkpoint SleepCheckpoint `json:"checkpoint"`
}

// Validate checks a checkpoint received from another host before it can replace VM state.
func (sleep *SleepMigration) Validate(specificationGeneration, restartGeneration uint64) error {
	record := sleep.Checkpoint
	switch sleep.State {
	case SleepStateDeepSleeping:
	case SleepStateComatose:
		if record.Disk == nil {
			return fmt.Errorf("migration sleep checkpoint is missing its disk")
		}
	default:
		return fmt.Errorf("invalid migration sleep state")
	}
	if record.SourceUserID == 0 ||
		record.SpecificationGeneration != specificationGeneration || record.RestartGeneration != restartGeneration {
		return fmt.Errorf("invalid migration sleep checkpoint identity")
	}
	if err := record.validate(); err != nil {
		return err
	}
	return record.validateMigrationArtifacts()
}

func (record SleepCheckpoint) validateMigrationArtifacts() error {
	for _, artifact := range []*sleepArtifactRecord{record.Memory, record.Disk} {
		if artifact == nil {
			continue
		}
		if artifact.Artifact == nil || artifact.ObjectKey == "" || artifact.validate(true) != nil {
			return fmt.Errorf("migration sleep artifact is not acknowledged or has no object key")
		}
	}
	return nil
}

// ReadSleepMigration captures a stable, acknowledged checkpoint under the operation lock.
func (host *MigrationHost) ReadSleepMigration(identifier string) (*SleepMigration, error) {
	desired, observed, err := host.manager.newVirtualMachine(identifier).records()
	if err != nil || !observed.hasRemoteSleepState() {
		return nil, err
	}
	record, err := host.manager.readSleepRecord(identifier)
	if err != nil {
		return nil, err
	}
	if record == nil || !record.canUpload(desired, observed) {
		return nil, fmt.Errorf("sleep checkpoint is not ready for migration: %w", ErrConflict)
	}
	sleep := &SleepMigration{State: observed.SleepState, Checkpoint: record.forMigration(desired.UserID)}
	if err := sleep.Validate(desired.SpecificationGeneration, desired.RestartGeneration); err != nil {
		return nil, fmt.Errorf("capture sleep migration: %w: %w", err, ErrConflict)
	}
	return sleep, nil
}

func (record SleepCheckpoint) forMigration(userID uint32) SleepCheckpoint {
	if record.Disk != nil && record.Disk.Artifact == nil {
		record.Disk = nil
	}
	for _, artifact := range []*sleepArtifactRecord{record.Memory, record.Disk} {
		if artifact != nil {
			artifact.SleepArtifactTarget = artifact.withoutUploadURLs()
		}
	}
	if record.SourceUserID == 0 {
		record.SourceUserID = userID
	}
	return record
}

// ApplySleepMigration persists a checkpoint and leaves its VM stopped without runtime calls.
func (host *MigrationHost) ApplySleepMigration(identifier string, sleep *SleepMigration) error {
	desired, observed, err := host.manager.newVirtualMachine(identifier).records()
	if err != nil {
		return err
	}
	if err := host.manager.writeSleepRecord(identifier, &sleep.Checkpoint); err != nil {
		return err
	}
	return host.manager.writeAppliedState(desired, &observed, StateStopped, sleep.State)
}
