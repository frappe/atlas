package firecracker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	savedStateDirectoryName        = "saved-state"
	pendingSavedStateDirectoryName = "saved-state-pending"
	savedStateMetadataFileName     = "metadata.json"
)

var errSavedStateNotFound = errors.New("saved VM state not found")

type savedStateRequirement struct {
	VirtualMachineID         string
	UserID                   uint32
	SpecificationGeneration  uint64
	RestartGeneration        uint64
	RescueGeneration         uint64
	FirecrackerCompatibility string
}

type savedStateMetadata struct {
	VirtualMachineID         string    `json:"virtual_machine_id"`
	UserID                   uint32    `json:"user_id"`
	SpecificationGeneration  uint64    `json:"specification_generation"`
	RestartGeneration        uint64    `json:"restart_generation"`
	RescueGeneration         uint64    `json:"rescue_generation"`
	FirecrackerCompatibility string    `json:"firecracker_compatibility"`
	CreatedAt                time.Time `json:"created_at"`
	StateFileName            string    `json:"state_file_name"`
	MemoryFileName           string    `json:"memory_file_name"`
	StateFileSizeBytes       int64     `json:"state_file_size_bytes"`
	MemoryFileSizeBytes      int64     `json:"memory_file_size_bytes"`
}

type savedState struct {
	Metadata   savedStateMetadata
	StatePath  string
	MemoryPath string
}

func (configuration Config) savedStateDirectory(id string) string {
	return filepath.Join(configuration.vmDir(id), savedStateDirectoryName)
}

func (configuration Config) pendingSavedStateDirectory(id string) string {
	return filepath.Join(configuration.chrootRoot(id), pendingSavedStateDirectoryName)
}

func (configuration Config) loadSavedState(requirement savedStateRequirement) (savedState, error) {
	directory := configuration.savedStateDirectory(requirement.VirtualMachineID)
	data, err := os.ReadFile(filepath.Join(directory, savedStateMetadataFileName))
	if errors.Is(err, fs.ErrNotExist) {
		return savedState{}, errSavedStateNotFound
	}
	if err != nil {
		return savedState{}, fmt.Errorf("read saved state metadata: %w", err)
	}
	metadata, err := decodeSavedStateMetadata(data)
	if err != nil {
		return savedState{}, err
	}
	if err := metadata.validate(requirement); err != nil {
		return savedState{}, err
	}
	statePath, err := validateSavedStateFile(directory, memorySnapshotStateFileName, metadata.StateFileSizeBytes)
	if err != nil {
		return savedState{}, err
	}
	memoryPath, err := validateSavedStateFile(directory, memorySnapshotMemoryFileName, metadata.MemoryFileSizeBytes)
	if err != nil {
		return savedState{}, err
	}
	return savedState{Metadata: metadata, StatePath: statePath, MemoryPath: memoryPath}, nil
}

func (runtime *Runtime) createSavedState(ctx context.Context, machine vm.RuntimeMachine) (savedState, error) {
	pending := runtime.configuration.pendingSavedStateDirectory(machine.ID)
	if err := os.RemoveAll(pending); err != nil {
		return savedState{}, fmt.Errorf("remove pending saved state: %w", err)
	}
	if _, _, err := runtime.createFullMemorySnapshot(ctx, machine, pendingSavedStateDirectoryName); err != nil {
		return savedState{}, err
	}

	stateSize, err := savedStateFileSize(filepath.Join(pending, memorySnapshotStateFileName))
	if err != nil {
		return savedState{}, err
	}
	memorySize, err := savedStateFileSize(filepath.Join(pending, memorySnapshotMemoryFileName))
	if err != nil {
		return savedState{}, err
	}
	metadata := savedStateMetadata{
		VirtualMachineID:         machine.ID,
		UserID:                   machine.UserID,
		SpecificationGeneration:  machine.SpecificationGeneration,
		RestartGeneration:        machine.RestartGeneration,
		RescueGeneration:         machine.RescueGeneration,
		FirecrackerCompatibility: runtime.firecrackerCompatibility(),
		CreatedAt:                time.Now().UTC(),
		StateFileName:            memorySnapshotStateFileName,
		MemoryFileName:           memorySnapshotMemoryFileName,
		StateFileSizeBytes:       stateSize,
		MemoryFileSizeBytes:      memorySize,
	}
	data, err := json.MarshalIndent(metadata, "", "  ")
	if err != nil {
		return savedState{}, fmt.Errorf("encode saved state metadata: %w", err)
	}
	if err := platform.WriteFile(filepath.Join(pending, savedStateMetadataFileName), data, 0o640); err != nil {
		return savedState{}, fmt.Errorf("write saved state metadata: %w", err)
	}

	published := runtime.configuration.savedStateDirectory(machine.ID)
	if err := os.Rename(pending, published); err != nil {
		return savedState{}, fmt.Errorf("publish saved VM state: %w", err)
	}
	return runtime.configuration.loadSavedState(runtime.newMachine(machine).savedStateRequirement())
}

func (runtime *Runtime) removeSavedState(id string) error {
	if err := os.RemoveAll(runtime.configuration.savedStateDirectory(id)); err != nil {
		return fmt.Errorf("remove saved VM state: %w", err)
	}
	return nil
}

func (metadata savedStateMetadata) validate(requirement savedStateRequirement) error {
	switch {
	case metadata.VirtualMachineID != requirement.VirtualMachineID:
		return fmt.Errorf("saved state VM ID %q does not match %q", metadata.VirtualMachineID, requirement.VirtualMachineID)
	case metadata.UserID != requirement.UserID:
		return fmt.Errorf("saved state user ID %d does not match %d", metadata.UserID, requirement.UserID)
	case metadata.SpecificationGeneration != requirement.SpecificationGeneration:
		return fmt.Errorf("saved state specification generation %d does not match %d", metadata.SpecificationGeneration, requirement.SpecificationGeneration)
	case metadata.RestartGeneration != requirement.RestartGeneration:
		return fmt.Errorf("saved state restart generation %d does not match %d", metadata.RestartGeneration, requirement.RestartGeneration)
	case metadata.RescueGeneration != requirement.RescueGeneration:
		return fmt.Errorf("saved state rescue generation %d does not match %d", metadata.RescueGeneration, requirement.RescueGeneration)
	case metadata.FirecrackerCompatibility != requirement.FirecrackerCompatibility:
		return fmt.Errorf("saved state Firecracker compatibility %q does not match %q", metadata.FirecrackerCompatibility, requirement.FirecrackerCompatibility)
	case metadata.StateFileName != memorySnapshotStateFileName || metadata.MemoryFileName != memorySnapshotMemoryFileName:
		return errors.New("saved state metadata has unexpected file names")
	case metadata.StateFileSizeBytes <= 0 || metadata.MemoryFileSizeBytes <= 0:
		return errors.New("saved state metadata has an invalid file size")
	case metadata.CreatedAt.IsZero():
		return errors.New("saved state metadata has no creation time")
	}
	return nil
}

func decodeSavedStateMetadata(data []byte) (savedStateMetadata, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var metadata savedStateMetadata
	if err := decoder.Decode(&metadata); err != nil {
		return savedStateMetadata{}, fmt.Errorf("decode saved state metadata: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return savedStateMetadata{}, errors.New("decode saved state metadata: unexpected trailing data")
	}
	return metadata, nil
}

func validateSavedStateFile(directory, name string, expectedSize int64) (string, error) {
	path := filepath.Join(directory, name)
	info, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("inspect saved state file %s: %w", name, err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("saved state file %s is not a regular file", name)
	}
	if info.Size() <= 0 || info.Size() != expectedSize {
		return "", fmt.Errorf("saved state file %s has size %d, expected %d", name, info.Size(), expectedSize)
	}
	return path, nil
}

func savedStateFileSize(path string) (int64, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return 0, fmt.Errorf("inspect saved state file %s: %w", filepath.Base(path), err)
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 {
		return 0, fmt.Errorf("saved state file %s is not a nonempty regular file", filepath.Base(path))
	}
	return info.Size(), nil
}
