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

// Part sizes follow the object storage limits for one upload.
const (
	minimumSleepPartSizeBytes = 5 << 20
	maximumSleepPartSizeBytes = 5 << 30
	maximumSleepParts         = 1000
)

// SleepArtifactTarget supplies object storage access for one sleep artifact:
// either one object, or Parts of PartSizeBytes for an artifact larger than one upload.
type SleepArtifactTarget struct {
	ObjectKey     string              `json:"object_key,omitempty"`
	UploadURL     string              `json:"upload_url,omitempty"`
	DownloadURL   string              `json:"download_url,omitempty"`
	PartSizeBytes int64               `json:"part_size_bytes,omitempty"`
	Parts         []SleepArtifactPart `json:"parts,omitempty"`
}

// SleepArtifactPart signs one consecutive object of an artifact.
type SleepArtifactPart struct {
	UploadURL   string `json:"upload_url,omitempty"`
	DownloadURL string `json:"download_url"`
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
}

type sleepArtifactRecord struct {
	SleepArtifactTarget
	Artifact *SleepArtifact `json:"artifact,omitempty"`
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
	if request.SnapshotID != "" && !validIdentifier(request.SnapshotID) {
		return fmt.Errorf("snapshot_id is not a valid identifier")
	}
	for _, target := range []*SleepArtifactTarget{request.Memory, request.Disk} {
		if target == nil {
			continue
		}
		if err := target.validate(refresh); err != nil {
			return err
		}
	}
	return nil
}

func (target SleepArtifactTarget) validate(refresh bool) error {
	if len(target.Parts) == 0 {
		return validateSleepURLs(target.UploadURL, target.DownloadURL, refresh)
	}
	if target.UploadURL != "" || target.DownloadURL != "" {
		return fmt.Errorf("parts replace upload_url and download_url")
	}
	if len(target.Parts) > maximumSleepParts {
		return fmt.Errorf("an artifact can have at most %d parts", maximumSleepParts)
	}
	if !refresh && (target.PartSizeBytes < minimumSleepPartSizeBytes || target.PartSizeBytes > maximumSleepPartSizeBytes) {
		return fmt.Errorf("part_size_bytes must be from 5 MiB to 5 GiB")
	}
	for _, part := range target.Parts {
		if err := validateSleepURLs(part.UploadURL, part.DownloadURL, refresh); err != nil {
			return err
		}
	}
	return nil
}

func validateSleepURLs(uploadURL, downloadURL string, refresh bool) error {
	if !validSleepURL(downloadURL) {
		return fmt.Errorf("download_url must be an absolute HTTP or HTTPS URL without credentials or a fragment")
	}
	if refresh {
		if uploadURL != "" {
			return fmt.Errorf("a URL refresh cannot supply upload_url")
		}
	} else if !validSleepURL(uploadURL) {
		return fmt.Errorf("upload_url must be an absolute HTTP or HTTPS URL without credentials or a fragment")
	}
	return nil
}

// sameObjects reports whether target signs the objects already stored. Signatures
// can rotate; a checkpoint cannot change object identity.
func (stored SleepArtifactTarget) sameObjects(target SleepArtifactTarget, refresh bool) bool {
	if len(stored.Parts) != len(target.Parts) || (!refresh && stored.PartSizeBytes != target.PartSizeBytes) {
		return false
	}
	if len(stored.Parts) == 0 {
		return sameSleepObject(stored.DownloadURL, target.DownloadURL) && (refresh || sameSleepObject(stored.UploadURL, target.UploadURL))
	}
	for index, part := range stored.Parts {
		if !sameSleepObject(part.DownloadURL, target.Parts[index].DownloadURL) ||
			(!refresh && !sameSleepObject(part.UploadURL, target.Parts[index].UploadURL)) {
			return false
		}
	}
	return true
}

// replaceURLs stores new signatures for the same objects.
func (stored *SleepArtifactTarget) replaceURLs(target SleepArtifactTarget, refresh bool) {
	stored.DownloadURL = target.DownloadURL
	if !refresh {
		stored.UploadURL = target.UploadURL
	}
	for index := range stored.Parts {
		stored.Parts[index].DownloadURL = target.Parts[index].DownloadURL
		if !refresh {
			stored.Parts[index].UploadURL = target.Parts[index].UploadURL
		}
	}
}

// DownloadURLs returns the URLs that read the artifact, in order.
func (target SleepArtifactTarget) DownloadURLs() []string {
	if len(target.Parts) == 0 {
		return []string{target.DownloadURL}
	}
	urls := make([]string, len(target.Parts))
	for index, part := range target.Parts {
		urls[index] = part.DownloadURL
	}
	return urls
}

// withoutUploadURLs returns a copy that can only read the artifact.
func (target SleepArtifactTarget) withoutUploadURLs() SleepArtifactTarget {
	target.UploadURL = ""
	parts := make([]SleepArtifactPart, len(target.Parts))
	for index, part := range target.Parts {
		parts[index] = SleepArtifactPart{DownloadURL: part.DownloadURL}
	}
	if len(parts) == 0 {
		parts = nil
	}
	target.Parts = parts
	return target
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
			// The controller can name a new checkpoint, so it can name its objects after it.
			identifier := request.SnapshotID
			if identifier == "" {
				identifier = newOperationID()
			}
			record = &SleepCheckpoint{ID: identifier, Identity: saved.Identity,
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
		if !(*pair.stored).sameObjects(*pair.target, refresh) {
			return ErrConflict
		}
		(*pair.stored).replaceURLs(*pair.target, refresh)
		if pair.target.ObjectKey != "" {
			if (*pair.stored).ObjectKey != "" && (*pair.stored).ObjectKey != pair.target.ObjectKey {
				return ErrConflict
			}
			(*pair.stored).ObjectKey = pair.target.ObjectKey
		}
	}
	return manager.writeSleepRecord(identifier, record)
}

func sameSleepObject(first, second string) bool {
	left, leftError := url.Parse(first)
	right, rightError := url.Parse(second)
	return leftError == nil && rightError == nil && left.Scheme == right.Scheme && left.Host == right.Host && left.EscapedPath() == right.EscapedPath()
}

// saveAndStop saves guest memory locally and forgets the previous checkpoint, so
// Metal never reports a checkpoint that the new saved state replaced.
func (manager *Manager) saveAndStop(ctx context.Context, machine RuntimeMachine) error {
	if err := manager.runtime.SaveAndStop(ctx, machine); err != nil {
		return err
	}
	err := os.Remove(filepath.Join(manager.store.virtualMachineDirectory(machine.ID), "sleep-state.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
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
	if artifact.Artifact == nil {
		return nil
	}
	digest, err := hex.DecodeString(artifact.Artifact.SHA256)
	if err != nil || len(digest) != 32 || artifact.Artifact.SizeBytes <= 0 || artifact.Artifact.StoredSizeBytes <= 0 ||
		artifact.Artifact.PartCount != len(artifact.Parts) {
		return fmt.Errorf("invalid sleep artifact content")
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
	return status
}

func (observed ObservedRecord) hasRemoteSleepState() bool {
	return observed.SleepState == SleepStateDeepSleeping || observed.SleepState == SleepStateComa
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
		record.Memory.Artifact == nil || (observed.SleepState == SleepStateComa && (record.Disk == nil || record.Disk.Artifact == nil)) {
		return fmt.Errorf("sleep checkpoint is missing or does not match the VM")
	}
	if observed.SleepState == SleepStateComa {
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
	if err := manager.snapshots.DownloadSleepMemory(ctx, directory, record.Memory.DownloadURLs(), *record.Memory.Artifact); err != nil {
		return fmt.Errorf("restore sleep memory: %w", err)
	}
	machine.SavedStateSourceUserID = record.SourceUserID
	return manager.runtime.InstallSavedState(ctx, machine, directory, record.Identity)
}

func (manager *Manager) restoreSleepDisk(ctx context.Context, identifier string, record *SleepCheckpoint) error {
	if record == nil || record.Disk == nil || record.Disk.Artifact == nil {
		return fmt.Errorf("sleep disk checkpoint is missing")
	}
	if err := manager.snapshots.RestoreSleepDisk(ctx, identifier, record.Disk.DownloadURLs(), *record.Disk.Artifact); err != nil {
		return fmt.Errorf("restore sleep disk: %w", err)
	}
	return nil
}
