package vm

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

type sleepUpload struct {
	memory   bool
	progress SleepUploadProgress
}

// OffloadSleepArtifacts serializes transfers for one VM without occupying its reconciler.
func (manager *Manager) OffloadSleepArtifacts(ctx context.Context, identifier string) error {
	unlock, err := manager.sleepTransferLocks.Lock(ctx, identifier)
	if err != nil {
		return err
	}
	defer unlock()
	return manager.offloadSleepArtifacts(ctx, identifier)
}

func (manager *Manager) hasPendingSleepTransfer(desired DesiredRecord, observed ObservedRecord) (bool, error) {
	record, err := manager.readSleepRecord(desired.ID)
	if err != nil || record == nil {
		return false, err
	}
	return record.canUpload(desired, observed) && (record.Memory.Artifact == nil ||
		(record.Disk != nil && record.Disk.Artifact == nil) || observed.OperationID == record.ID), nil
}

func (manager *Manager) maintainSleepTransfer(ctx context.Context, desired DesiredRecord, observed ObservedRecord) error {
	// Reattach traffic hooks after restart without replacing the upload's phase or error.
	networkInterface, err := manager.network.Ensure(ctx, manager.networkRequest(desired))
	if err != nil {
		return err
	}
	observed.NetworkInterface = networkInterface
	if manager.traffic != nil {
		target := traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}
		if err := manager.traffic.StartWatching(target); err != nil {
			return fmt.Errorf("watch traffic for VM %s: %w", desired.ID, err)
		}
	}
	return manager.store.writeObserved(desired.ID, observed)
}

func (upload *sleepUpload) phase() string {
	phase := "compress-sleep-"
	if upload.progress.TotalBytes.Load() > 0 {
		phase = "upload-sleep-"
	}
	if upload.memory {
		return phase + "memory"
	}
	return phase + "disk"
}

func (manager *Manager) applySleepUpload(snapshotID string, information *Information) {
	manager.sleepUploadsMutex.Lock()
	upload := manager.sleepUploads[snapshotID]
	manager.sleepUploadsMutex.Unlock()
	if upload == nil {
		return
	}
	status := information.SleepSnapshot.Disk
	if upload.memory {
		status = information.SleepSnapshot.Memory
	}
	if !status.Uploaded {
		if total := upload.progress.TotalBytes.Load(); total > 0 {
			// Reading the complete body is not an object-store acknowledgment.
			percent := int(min(99, upload.progress.UploadedBytes.Load()*100/total))
			status.UploadPercent = &percent
		}
	}
	if information.OperationID == snapshotID &&
		(information.Phase == "compress-sleep-memory" || information.Phase == "compress-sleep-disk") {
		information.Phase = upload.phase()
	}
}

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
	if !record.canUpload(desired, observed) || target == nil {
		unlock()
		return nil
	}
	directory := filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-transfer", record.ID)
	if target.Artifact != nil {
		defer unlock()
		if memory {
			if err := os.RemoveAll(directory); err != nil {
				return err
			}
			return manager.evictSleepArtifacts(ctx, desired, &observed, record)
		}
		return manager.snapshots.DeleteSnapshot(ctx, record.ID)
	}
	if !memory && record.Memory.Artifact == nil {
		unlock()
		return ErrConflict
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil || status.State != StateStopped {
		unlock()
		return err
	}
	var saved SavedState
	if status.HasSavedState {
		saved, err = manager.runtime.SavedState(ctx, machine)
		if err == nil && saved.Identity != record.Identity {
			err = ErrConflict
		}
	} else if memory || !observed.hasRemoteSleepState() {
		err = ErrConflict
	}
	if err != nil {
		unlock()
		return err
	}
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
	artifactName := "disk"
	if memory {
		artifactName = "memory"
	}
	if err := manager.runOperation(ctx, identifier, &observed, record.ID, "prepare-sleep-"+artifactName, func() error {
		if memory {
			return manager.snapshots.StageSleepMemory(ctx, saved.Directory, directory)
		}
		return manager.snapshots.StageSleepDisk(ctx, SnapshotRequest{VirtualMachineID: identifier, KernelSHA256: desired.Specification.Image.KernelSHA256}, record.ID)
	}); err != nil {
		unlock()
		return err
	}
	observed.Phase = "compress-sleep-" + artifactName
	if err := manager.store.writeObserved(identifier, observed); err != nil {
		unlock()
		return err
	}
	upload := &sleepUpload{memory: memory}
	manager.sleepUploadsMutex.Lock()
	manager.sleepUploads[record.ID] = upload
	manager.sleepUploadsMutex.Unlock()
	defer func() {
		manager.sleepUploadsMutex.Lock()
		delete(manager.sleepUploads, record.ID)
		manager.sleepUploadsMutex.Unlock()
	}()
	destination := target.UploadURL
	unlock()

	// The upload reads only staging, so wake does not wait for its network transfer.
	var artifact SleepArtifact
	if memory {
		artifact, err = manager.snapshots.UploadSleepMemory(ctx, directory, destination, &upload.progress)
	} else {
		artifact, err = manager.snapshots.UploadSleepDisk(ctx, record.ID, destination, &upload.progress)
	}
	return manager.finishSleepUpload(ctx, identifier, record.ID, memory, artifact, upload.phase(), err)
}

func (manager *Manager) finishSleepUpload(ctx context.Context, identifier, snapshotID string, memory bool, artifact SleepArtifact, phase string, uploadError error) error {
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
	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return err
	}
	if uploadError != nil {
		if !current.canUpload(desired, observed) {
			return uploadError
		}
		return manager.runOperation(completion, identifier, &observed, current.ID, phase, func() error { return uploadError })
	}
	current.target(memory).Artifact = &artifact
	if err := manager.writeSleepRecord(identifier, current); err != nil {
		return err
	}
	if err := manager.evictSleepArtifacts(completion, desired, &observed, current); err != nil {
		return err
	}
	if observed.OperationID == snapshotID {
		observed.completeOperation()
		return manager.store.writeObserved(identifier, observed)
	}
	return nil
}

func (manager *Manager) evictSleepArtifacts(ctx context.Context, desired DesiredRecord, observed *ObservedRecord, record *sleepRecord) error {
	if manager.isMigrating(desired.ID) || !record.canUpload(desired, *observed) || record.Memory.Artifact == nil {
		return nil
	}
	machine := runtimeMachine(desired, observed.NetworkInterface)
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil || status.State != StateStopped {
		return err
	}
	if observed.SleepState == SleepStateSleeping {
		observed.SleepState = SleepStateDeepSleeping
	}
	if status.HasSavedState {
		saved, err := manager.runtime.SavedState(ctx, machine)
		if err != nil {
			return err
		}
		if saved.Identity != record.Identity {
			return ErrConflict
		}
		if err := manager.runOperation(ctx, desired.ID, observed, record.ID, "evict-sleep-memory", func() error {
			return manager.runtime.DeleteSavedState(ctx, machine)
		}); err != nil {
			return err
		}
	}
	if record.Disk != nil && record.Disk.Artifact != nil {
		hasDisk, err := manager.storage.HasDisk(ctx, desired.ID)
		if err != nil {
			return err
		}
		observed.SleepState = SleepStateComatose
		if hasDisk {
			if err := manager.runOperation(ctx, desired.ID, observed, record.ID, "evict-sleep-disk", func() error {
				return manager.storage.Release(ctx, desired.ID)
			}); err != nil {
				return err
			}
		}
	}
	observed.completeOperation()
	return manager.store.writeObserved(desired.ID, *observed)
}
