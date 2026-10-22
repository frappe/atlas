package vm

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"

	"github.com/frappe/atlas/metal/internal/platform"
)

// SleepArtifactTarget supplies object storage access for one sleep artifact.
type SleepArtifactTarget struct {
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

// SleepArtifactStatus reports an uploaded artifact's verified content.
type SleepArtifactStatus struct {
	Uploaded bool           `json:"uploaded"`
	Artifact *SleepArtifact `json:"artifact,omitempty"`
}

type sleepArtifactRecord struct {
	SleepArtifactTarget
	Artifact *SleepArtifact `json:"artifact,omitempty"`
}

type sleepRecord struct {
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
			if target.UploadURL != "" {
				return fmt.Errorf("a URL refresh cannot supply upload_url")
			}
		} else if !validSleepURL(target.UploadURL) {
			return fmt.Errorf("upload_url must be an absolute HTTP or HTTPS URL without credentials or a fragment")
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
	if !refresh {
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
			record = &sleepRecord{ID: newOperationID(), Identity: saved.Identity,
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
		// Signatures can rotate; a checkpoint cannot change object identity.
		if !sameSleepObject((*pair.stored).DownloadURL, pair.target.DownloadURL) ||
			(!refresh && !sameSleepObject((*pair.stored).UploadURL, pair.target.UploadURL)) {
			return ErrConflict
		}
		(*pair.stored).DownloadURL = pair.target.DownloadURL
		if !refresh {
			(*pair.stored).UploadURL = pair.target.UploadURL
		}
	}
	return manager.writeSleepRecord(identifier, record)
}

func sameSleepObject(first, second string) bool {
	left, leftError := url.Parse(first)
	right, rightError := url.Parse(second)
	return leftError == nil && rightError == nil && left.Scheme == right.Scheme && left.Host == right.Host && left.EscapedPath() == right.EscapedPath()
}

func (manager *Manager) readSleepRecord(identifier string) (*sleepRecord, error) {
	var record sleepRecord
	err := readRecord(filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-state.json"), &record)
	if errors.Is(err, ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !validIdentifier(record.ID) || record.Identity == "" || record.Memory == nil {
		return nil, fmt.Errorf("invalid sleep-state record")
	}
	for _, target := range []*sleepArtifactRecord{record.Memory, record.Disk} {
		if target == nil {
			continue
		}
		if target.Artifact != nil {
			digest, err := hex.DecodeString(target.Artifact.SHA256)
			if err != nil || len(digest) != 32 || target.Artifact.SizeBytes <= 0 || target.Artifact.StoredSizeBytes <= 0 {
				return nil, fmt.Errorf("invalid sleep artifact content")
			}
		}
	}
	return &record, nil
}

func (record *sleepRecord) canUpload(desired DesiredRecord, observed ObservedRecord) bool {
	return record.matchesDesired(desired) && desired.Specification.SleepAfterIdleSeconds > 0 &&
		observed.State == StateStopped && observed.SleepState != SleepStateAwake
}

func (record *sleepRecord) matchesDesired(desired DesiredRecord) bool {
	return desired.State == StateRunning && desired.SpecificationGeneration == record.SpecificationGeneration && desired.RestartGeneration == record.RestartGeneration
}

func (record *sleepRecord) target(memory bool) *sleepArtifactRecord {
	if memory {
		return record.Memory
	}
	return record.Disk
}

func (manager *Manager) writeSleepRecord(identifier string, record *sleepRecord) error {
	path := filepath.Join(manager.store.virtualMachineDirectory(identifier), "sleep-state.json")
	data, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	return platform.WriteFile(path, data, 0o600)
}

func (record *sleepRecord) status() *SleepSnapshotStatus {
	status := &SleepSnapshotStatus{ID: record.ID}
	if record.Memory != nil {
		status.Memory = &SleepArtifactStatus{Uploaded: record.Memory.Artifact != nil, Artifact: record.Memory.Artifact}
	}
	if record.Disk != nil {
		status.Disk = &SleepArtifactStatus{Uploaded: record.Disk.Artifact != nil, Artifact: record.Disk.Artifact}
	}
	return status
}
