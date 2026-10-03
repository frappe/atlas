package firecracker

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func savedStateFixture(t *testing.T, configuration Config, requirement savedStateRequirement) savedStateMetadata {
	t.Helper()
	directory := configuration.savedStateDirectory(requirement.VirtualMachineID)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	stateSize := writeSavedStateFile(t, filepath.Join(directory, memorySnapshotStateFileName), 128)
	memorySize := writeSavedStateFile(t, filepath.Join(directory, memorySnapshotMemoryFileName), 256)
	metadata := savedStateMetadata{
		VirtualMachineID: requirement.VirtualMachineID, UserID: requirement.UserID,
		SpecificationGeneration:  requirement.SpecificationGeneration,
		RestartGeneration:        requirement.RestartGeneration,
		RescueGeneration:         requirement.RescueGeneration,
		FirecrackerCompatibility: requirement.FirecrackerCompatibility,
		CreatedAt:                time.Now().UTC(), StateFileName: memorySnapshotStateFileName,
		MemoryFileName: memorySnapshotMemoryFileName, StateFileSizeBytes: stateSize,
		MemoryFileSizeBytes: memorySize,
	}
	data, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, savedStateMetadataFileName), data, 0o640); err != nil {
		t.Fatal(err)
	}
	return metadata
}

func writeSavedStateFile(t *testing.T, path string, size int) int64 {
	t.Helper()
	if err := os.WriteFile(path, make([]byte, size), 0o640); err != nil {
		t.Fatal(err)
	}
	return int64(size)
}

func TestSavedStateUsesOneFixedDirectory(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	if got := configuration.savedStateDirectory("vm-1"); got != filepath.Join(configuration.MachinesDir, "vm-1", "saved-state") {
		t.Fatalf("saved state directory = %s", got)
	}
	if got := configuration.pendingSavedStateDirectory("vm-1"); got != filepath.Join(configuration.chrootRoot("vm-1"), "saved-state-pending") {
		t.Fatalf("pending directory = %s", got)
	}
}

func TestLoadSavedStateValidatesMetadataAndFiles(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{
		VirtualMachineID: "vm-1", UserID: 1001, SpecificationGeneration: 2,
		RestartGeneration: 3, FirecrackerCompatibility: "firecracker-test",
	}
	savedStateFixture(t, configuration, requirement)
	state, err := configuration.loadSavedState(requirement)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(state.StatePath) != "state" || filepath.Base(state.MemoryPath) != "memory" {
		t.Fatalf("saved state paths = %s and %s", state.StatePath, state.MemoryPath)
	}
}

func TestLoadSavedStateRejectsAnIncompatibleGeneration(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1", UserID: 1001, SpecificationGeneration: 2}
	savedStateFixture(t, configuration, requirement)
	requirement.SpecificationGeneration = 3
	if _, err := configuration.loadSavedState(requirement); err == nil {
		t.Fatal("want an incompatible generation error")
	}
}

func TestLoadSavedStateRejectsUnknownMetadata(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1"}
	savedStateFixture(t, configuration, requirement)
	path := filepath.Join(configuration.savedStateDirectory(requirement.VirtualMachineID), savedStateMetadataFileName)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	data[len(data)-1] = ','
	data = append(data, []byte(`"unknown":true}`)...)
	if err := os.WriteFile(path, data, 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := configuration.loadSavedState(requirement); err == nil {
		t.Fatal("want an unknown metadata field error")
	}
}

func TestLoadSavedStateRejectsAMissingFile(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1"}
	savedStateFixture(t, configuration, requirement)
	path := filepath.Join(configuration.savedStateDirectory(requirement.VirtualMachineID), memorySnapshotMemoryFileName)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := configuration.loadSavedState(requirement); err == nil {
		t.Fatal("want a missing memory file error")
	}
}

func TestLoadSavedStateRejectsAChangedFileSize(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1"}
	savedStateFixture(t, configuration, requirement)
	path := filepath.Join(configuration.savedStateDirectory(requirement.VirtualMachineID), memorySnapshotStateFileName)
	if err := os.WriteFile(path, []byte("short"), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := configuration.loadSavedState(requirement); err == nil {
		t.Fatal("want a changed state file size error")
	}
}

func TestLoadSavedStateReportsAbsence(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	_, err := configuration.loadSavedState(savedStateRequirement{VirtualMachineID: "vm-1"})
	if !errors.Is(err, errSavedStateNotFound) {
		t.Fatalf("error = %v, want errSavedStateNotFound", err)
	}
}

func TestRemoveSavedStateKeepsVMRecords(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1"}
	savedStateFixture(t, configuration, requirement)
	configPath := filepath.Join(configuration.vmDir("vm-1"), "config.json")
	if err := os.WriteFile(configPath, []byte("{}"), 0o640); err != nil {
		t.Fatal(err)
	}
	runtime := &Runtime{configuration: configuration}
	if err := runtime.removeSavedState("vm-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(configPath); err != nil {
		t.Fatal(err)
	}
}

func TestSavedStateCannotCrossRescueSessions(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir()}
	requirement := savedStateRequirement{VirtualMachineID: "vm-1", RescueGeneration: 2}
	savedStateFixture(t, configuration, requirement)
	if _, err := configuration.loadSavedState(requirement); err != nil {
		t.Fatal(err)
	}
	requirement.RescueGeneration = 4
	if _, err := configuration.loadSavedState(requirement); err == nil {
		t.Fatal("restored memory from an earlier rescue session")
	}
}
