package vm

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"time"
)

func (manager *Manager) offloadSleepArtifacts(ctx context.Context, identifier string) error {
	for _, memory := range []bool{true, false} {
		if err := manager.offloadSleepArtifact(ctx, identifier, memory); err != nil {
			return err
		}
	}
	return nil
}

func (manager *Manager) offloadSleepArtifact(ctx context.Context, identifier string, memory bool) error {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return err
	}
	if manager.isMigrating(identifier) {
		unlock()
		return nil
	}
	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		unlock()
		return err
	}
	record, err := manager.readSleepRecord(identifier)
	if err != nil || record == nil {
		unlock()
		return err
	}
	target := record.target(memory)
	if !record.canUpload(desired, observed) || target == nil || target.Artifact != nil {
		unlock()
		return nil
	}
	if !memory && record.Memory.Artifact == nil {
		unlock()
		return ErrConflict
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil || status.State != StateStopped || !status.HasSavedState {
		unlock()
		return err
	}
	saved, err := manager.runtime.SavedState(ctx, machine)
	if err != nil || saved.Identity != record.Identity {
		unlock()
		return err
	}
	directory := filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-transfer", record.ID)
	defer os.RemoveAll(directory)
	if !memory {
		defer func() {
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
			defer cancel()
			if err := manager.snapshots.DeleteSnapshot(cleanup, record.ID); err != nil {
				manager.logger.ErrorContext(cleanup, "remove sleep disk staging", "virtual_machine_id", identifier, "error", err)
			}
		}()
	}
	phase := "upload-sleep-disk"
	if memory {
		phase = "upload-sleep-memory"
	}
	if err := manager.runOperation(ctx, identifier, &observed, record.ID, phase, func() error {
		if memory {
			return manager.snapshots.StageSleepMemory(ctx, saved.Directory, directory)
		}
		return manager.snapshots.StageSleepDisk(ctx, SnapshotRequest{VirtualMachineID: identifier, KernelSHA256: desired.Specification.Image.KernelSHA256}, record.ID)
	}); err != nil {
		unlock()
		return err
	}
	destination := target.UploadURL
	unlock()

	// The upload reads only staging, so the existing wake path can use local state.
	var artifact SleepArtifact
	if memory {
		artifact, err = manager.snapshots.UploadSleepMemory(ctx, directory, destination)
	} else {
		artifact, err = manager.snapshots.UploadSleepDisk(ctx, record.ID, destination)
	}
	return manager.finishSleepUpload(ctx, identifier, record.ID, memory, artifact, err)
}

func (manager *Manager) finishSleepUpload(ctx context.Context, identifier, snapshotID string, memory bool, artifact SleepArtifact, uploadError error) error {
	completion, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	unlock, err := manager.operationLocks.Lock(completion, identifier)
	if err != nil {
		return errors.Join(uploadError, err)
	}
	defer unlock()
	current, err := manager.readSleepRecord(identifier)
	if err != nil || current == nil || current.ID != snapshotID {
		return err
	}
	_, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return err
	}
	if uploadError != nil {
		phase := "upload-sleep-disk"
		if memory {
			phase = "upload-sleep-memory"
		}
		return manager.runOperation(completion, identifier, &observed, current.ID, phase, func() error { return uploadError })
	}
	current.target(memory).Artifact = &artifact
	if err := manager.writeSleepRecord(identifier, current); err != nil {
		return err
	}
	if observed.OperationID == snapshotID {
		observed.completeOperation()
		return manager.store.writeObserved(identifier, observed)
	}
	return nil
}
