package vm

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

// SleepArtifactTarget supplies object storage access for one sleep artifact.
type SleepArtifactTarget struct {
	ObjectKey         string                `json:"object_key,omitempty"`
	DownloadURL       string                `json:"download_url"`
	Multipart         *SleepMultipartUpload `json:"multipart,omitempty"`
	CompletedUploadID string                `json:"completed_upload_id,omitempty"`
}

// SleepMultipartUpload supplies the same signed part plan used for image uploads.
type SleepMultipartUpload struct {
	UploadID    string            `json:"upload_id"`
	PartSizeMiB int64             `json:"part_size_mib"`
	Parts       []SleepUploadPart `json:"parts"`
}

// SleepUploadPart is one signed multipart destination.
type SleepUploadPart struct {
	PartNumber int    `json:"part_number"`
	URL        string `json:"url"`
}

// SleepStateRequest selects memory, disk, or both for one sleep checkpoint.
type SleepStateRequest struct {
	SnapshotID string               `json:"snapshot_id,omitempty"`
	Memory     *SleepArtifactTarget `json:"memory,omitempty"`
	Disk       *SleepArtifactTarget `json:"disk,omitempty"`
}

// SleepSnapshotStatus reports transfer state without object storage credentials.
type SleepSnapshotStatus struct {
	ID     string               `json:"id"`
	Memory *SleepArtifactStatus `json:"memory,omitempty"`
	Disk   *SleepArtifactStatus `json:"disk,omitempty"`
}

// SleepArtifactStatus reports upload progress and verified content.
type SleepArtifactStatus struct {
	ObjectKey     string         `json:"object_key,omitempty"`
	Uploaded      bool           `json:"uploaded"`
	Artifact      *SleepArtifact `json:"artifact,omitempty"`
	UploadPercent *int           `json:"upload_percent,omitempty"`
	UploadID      string         `json:"upload_id,omitempty"`
	Pending       *SleepArtifact `json:"pending,omitempty"`
}

type sleepArtifactRecord struct {
	SleepArtifactTarget
	Artifact *SleepArtifact `json:"artifact,omitempty"`
	Pending  *SleepArtifact `json:"pending,omitempty"`
}

// SleepCheckpoint holds the acknowledged artifacts and identity of one sleeping VM.
type SleepCheckpoint struct {
	SourceUserID            uint32               `json:"source_user_id,omitempty"`
	ID                      string               `json:"id"`
	Identity                string               `json:"identity"`
	SpecificationGeneration uint64               `json:"specification_generation"`
	RestartGeneration       uint64               `json:"restart_generation"`
	Memory                  *sleepArtifactRecord `json:"memory,omitempty"`
	Disk                    *sleepArtifactRecord `json:"disk,omitempty"`
}

// Validate checks URL syntax and the fields allowed by each sleep-state operation.
func (request SleepStateRequest) Validate(refresh bool) error {
	if request.Memory == nil && request.Disk == nil {
		return fmt.Errorf("memory or disk is required")
	}
	if refresh && request.SnapshotID == "" {
		return fmt.Errorf("snapshot_id is required when refreshing download URLs")
	}
	for _, target := range []*SleepArtifactTarget{request.Memory, request.Disk} {
		if target == nil {
			continue
		}
		if !validSleepURL(target.DownloadURL) {
			return fmt.Errorf("download_url must be an absolute HTTP or HTTPS URL without credentials or a fragment")
		}
		if refresh {
			if target.Multipart != nil {
				return fmt.Errorf("a URL refresh cannot supply upload destinations")
			}
		} else if target.Multipart != nil {
			if target.CompletedUploadID != "" {
				return fmt.Errorf("multipart cannot be combined with completed_upload_id")
			}
			if err := target.Multipart.validate(); err != nil {
				return err
			}
		} else {
			return fmt.Errorf("multipart is required for an upload")
		}
	}
	return nil
}

func (upload SleepMultipartUpload) validate() error {
	if upload.UploadID == "" || upload.PartSizeMiB < 5 || upload.PartSizeMiB > 5120 || len(upload.Parts) == 0 || len(upload.Parts) > 10000 {
		return fmt.Errorf("invalid multipart upload plan")
	}
	for index, part := range upload.Parts {
		if part.PartNumber != index+1 || !validSleepURL(part.URL) {
			return fmt.Errorf("multipart parts must be consecutive signed HTTP URLs")
		}
	}
	return nil
}

func validSleepURL(value string) bool {
	parsed, err := url.Parse(value)
	return err == nil && parsed.Host != "" && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.User == nil && parsed.Fragment == ""
}

// OffloadSleepState records snapshot upload destinations for an already sleeping VM.
func (manager *Manager) OffloadSleepState(ctx context.Context, identifier string, request SleepStateRequest) error {
	return manager.updateSleepState(ctx, identifier, request, false)
}

// RefreshSleepState stores GET URLs for the named checkpoint without downloading it.
func (manager *Manager) RefreshSleepState(ctx context.Context, identifier string, request SleepStateRequest) error {
	return manager.updateSleepState(ctx, identifier, request, true)
}

func (manager *Manager) updateSleepState(ctx context.Context, identifier string, request SleepStateRequest, refresh bool) error {
	unlock, err := manager.operationLocks.Lock(ctx, identifier)
	if err != nil {
		return err
	}
	defer unlock()
	if manager.isMigrating(identifier) {
		return ErrConflict
	}
	desired, observed, err := manager.newVirtualMachine(identifier).records()
	if err != nil {
		return err
	}
	if desired.State == StateDestroyed || (!refresh && (desired.State != StateRunning || desired.Specification.SleepAfterIdleSeconds <= 0 || observed.State != StateStopped || observed.SleepState == SleepStateAwake)) {
		return ErrConflict
	}
	record, err := manager.readSleepRecord(identifier)
	if err != nil {
		return err
	}
	if !refresh && !observed.hasRemoteSleepState() {
		saved, err := manager.runtime.SavedState(ctx, runtimeMachine(desired, observed.NetworkInterface))
		if err != nil {
			return err
		}
		if record != nil && record.Identity != saved.Identity {
			record = nil
		}
		if record == nil {
			if request.SnapshotID != "" {
				return ErrConflict
			}
			record = &SleepCheckpoint{ID: newOperationID(), Identity: saved.Identity,
				SpecificationGeneration: desired.SpecificationGeneration, RestartGeneration: desired.RestartGeneration}
		}
	}
	if record == nil {
		return ErrConflict
	}
	if (request.SnapshotID != "" && request.SnapshotID != record.ID) ||
		(!refresh && !record.matchesDesired(desired)) {
		return ErrConflict
	}
	if request.Disk != nil && record.Memory == nil && request.Memory == nil {
		return ErrConflict
	}
	acknowledged := false
	for _, pair := range []struct {
		target *SleepArtifactTarget
		stored **sleepArtifactRecord
	}{{request.Memory, &record.Memory}, {request.Disk, &record.Disk}} {
		if pair.target == nil {
			continue
		}
		if *pair.stored == nil {
			if refresh {
				return ErrConflict
			}
			*pair.stored = &sleepArtifactRecord{SleepArtifactTarget: *pair.target}
			continue
		}
		// Signatures can rotate; a checkpoint cannot change object identity.
		if !sameSleepObject((*pair.stored).DownloadURL, pair.target.DownloadURL) ||
			(!refresh && ((*pair.stored).Multipart == nil) != (pair.target.Multipart == nil)) ||
			(!refresh && pair.target.Multipart != nil && (*pair.stored).Multipart.UploadID != pair.target.Multipart.UploadID) {
			return ErrConflict
		}
		(*pair.stored).DownloadURL = pair.target.DownloadURL
		if pair.target.ObjectKey != "" {
			if (*pair.stored).ObjectKey != "" && (*pair.stored).ObjectKey != pair.target.ObjectKey {
				return ErrConflict
			}
			(*pair.stored).ObjectKey = pair.target.ObjectKey
		}
		if !refresh && (*pair.stored).Artifact == nil {
			(*pair.stored).Multipart = pair.target.Multipart
		}
		if pair.target.CompletedUploadID != "" {
			stored := *pair.stored
			if stored.Multipart == nil || stored.Multipart.UploadID != pair.target.CompletedUploadID || (stored.Pending == nil && stored.Artifact == nil) {
				return ErrConflict
			}
			if stored.Pending != nil {
				stored.Artifact = stored.Pending
				stored.Artifact.Parts = nil
				stored.Multipart.Parts = nil
				stored.Pending = nil
				acknowledged = true
			}
		}
	}
	if err := manager.writeSleepRecord(identifier, record); err != nil {
		return err
	}
	if acknowledged && record.canUpload(desired, observed) {
		observed.OperationID = record.ID
		observed.Phase = "evict-sleep-artifacts"
		return manager.store.writeObserved(identifier, observed)
	}
	return nil
}

func sameSleepObject(first, second string) bool {
	left, leftError := url.Parse(first)
	right, rightError := url.Parse(second)
	return leftError == nil && rightError == nil && left.Scheme == right.Scheme && left.Host == right.Host && left.EscapedPath() == right.EscapedPath()
}

func (manager *Manager) readSleepRecord(identifier string) (*SleepCheckpoint, error) {
	var record SleepCheckpoint
	err := readRecord(filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-state.json"), &record)
	if errors.Is(err, ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := record.validate(); err != nil {
		return nil, err
	}
	return &record, nil
}

func (record SleepCheckpoint) validate() error {
	if !validIdentifier(record.ID) || record.Identity == "" || record.Memory == nil {
		return fmt.Errorf("invalid sleep-state record")
	}
	for _, target := range []*sleepArtifactRecord{record.Memory, record.Disk} {
		if target == nil {
			continue
		}
		if err := target.validateContent(); err != nil {
			return err
		}
	}
	return nil
}

func (artifact sleepArtifactRecord) validateContent() error {
	for _, content := range []*SleepArtifact{artifact.Artifact, artifact.Pending} {
		if content == nil {
			continue
		}
		digest, err := hex.DecodeString(content.SHA256)
		if err != nil || len(digest) != 32 || content.SizeBytes <= 0 || content.StoredSizeBytes <= 0 {
			return fmt.Errorf("invalid sleep artifact content")
		}
	}
	return nil
}

func (record *SleepCheckpoint) canUpload(desired DesiredRecord, observed ObservedRecord) bool {
	return record.matchesDesired(desired) && desired.Specification.SleepAfterIdleSeconds > 0 &&
		desired.Generation == observed.Generation && observed.State == StateStopped && observed.SleepState != SleepStateAwake
}

func (record *SleepCheckpoint) matchesDesired(desired DesiredRecord) bool {
	return desired.State == StateRunning && desired.SpecificationGeneration == record.SpecificationGeneration && desired.RestartGeneration == record.RestartGeneration
}

func (record *SleepCheckpoint) target(memory bool) *sleepArtifactRecord {
	if memory {
		return record.Memory
	}
	return record.Disk
}

func (manager *Manager) writeSleepRecord(identifier string, record *SleepCheckpoint) error {
	path := filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-state.json")
	data, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	return platform.WriteFile(path, data, 0o600)
}

func (manager *Manager) removeSleepRecord(identifier string) error {
	err := os.Remove(filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-state.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func (record *SleepCheckpoint) status() *SleepSnapshotStatus {
	status := &SleepSnapshotStatus{ID: record.ID}
	if record.Memory != nil {
		status.Memory = &SleepArtifactStatus{Uploaded: record.Memory.Artifact != nil, Artifact: record.Memory.Artifact, ObjectKey: record.Memory.ObjectKey}
	}
	if record.Disk != nil {
		status.Disk = &SleepArtifactStatus{Uploaded: record.Disk.Artifact != nil, Artifact: record.Disk.Artifact, ObjectKey: record.Disk.ObjectKey}
	}
	for _, artifact := range []*SleepArtifactStatus{status.Memory, status.Disk} {
		if artifact != nil && artifact.Uploaded {
			percent := 100
			artifact.UploadPercent = &percent
		}
	}
	for _, pair := range []struct {
		record *sleepArtifactRecord
		status *SleepArtifactStatus
	}{{record.Memory, status.Memory}, {record.Disk, status.Disk}} {
		if pair.record != nil && pair.record.Multipart != nil {
			pair.status.UploadID = pair.record.Multipart.UploadID
			pair.status.Pending = pair.record.Pending
			if pair.record.Pending != nil {
				percent := 99
				pair.status.UploadPercent = &percent
			}
		}
	}
	return status
}

func (observed ObservedRecord) hasRemoteSleepState() bool {
	return observed.SleepState == SleepStateDeepSleeping || observed.SleepState == SleepStateComatose
}

func (manager *Manager) restoreSleepArtifacts(ctx context.Context, desired DesiredRecord, machine RuntimeMachine, observed ObservedRecord) error {
	if !observed.hasRemoteSleepState() {
		return nil
	}
	record, err := manager.readSleepRecord(desired.ID)
	if err != nil {
		return err
	}
	if record == nil || record.SpecificationGeneration != desired.SpecificationGeneration || record.RestartGeneration != desired.RestartGeneration ||
		record.Memory.Artifact == nil || (observed.SleepState == SleepStateComatose && (record.Disk == nil || record.Disk.Artifact == nil)) {
		return fmt.Errorf("sleep checkpoint is missing or does not match the VM")
	}
	if observed.SleepState == SleepStateComatose {
		if err := manager.restoreSleepDisk(ctx, desired.ID, record); err != nil {
			return err
		}
	}
	status, err := manager.runtime.Inspect(ctx, machine)
	if err != nil {
		return err
	}
	if status.HasSavedState {
		saved, err := manager.runtime.SavedState(ctx, machine)
		if err != nil {
			return err
		}
		if saved.Identity != record.Identity {
			return fmt.Errorf("local sleep checkpoint identity mismatch")
		}
		return nil
	}
	directory := filepath.Join(manager.store.virtualMachineDirectory(desired.ID), "sleep-restore")
	if err := os.RemoveAll(directory); err != nil {
		return err
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	if err := manager.snapshots.DownloadSleepMemory(ctx, directory, record.Memory.DownloadURL, *record.Memory.Artifact); err != nil {
		return fmt.Errorf("restore sleep memory: %w", err)
	}
	machine.SavedStateSourceUserID = record.SourceUserID
	return manager.runtime.InstallSavedState(ctx, machine, directory, record.Identity)
}

func (manager *Manager) restoreSleepDisk(ctx context.Context, identifier string, record *SleepCheckpoint) error {
	if record == nil || record.Disk == nil || record.Disk.Artifact == nil {
		return fmt.Errorf("sleep disk checkpoint is missing")
	}
	if err := manager.snapshots.RestoreSleepDisk(ctx, identifier, record.Disk.DownloadURL, *record.Disk.Artifact); err != nil {
		return fmt.Errorf("restore sleep disk: %w", err)
	}
	return nil
}
