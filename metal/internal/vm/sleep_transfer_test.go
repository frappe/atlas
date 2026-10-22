package vm

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSleepUploadProgressIsPerArtifactAndTransient(t *testing.T) {
	manager, _, _, snapshots := sleepyManager(t)
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	snapshots.duringStage = func() {
		information, err := manager.Information(t.Context(), "vm-1")
		if err != nil || !strings.HasPrefix(information.Phase, "prepare-sleep-") {
			t.Fatalf("preparation phase: %q, %v", information.Phase, err)
		}
	}
	snapshots.duringProgress = func(progress *SleepUploadProgress) {
		name := "memory"
		if snapshots.uploads == 2 {
			name = "disk"
		}
		information, err := manager.Information(t.Context(), "vm-1")
		if err != nil || information.Phase != "compress-sleep-"+name {
			t.Fatalf("compression phase: %q, %v", information.Phase, err)
		}
		status := information.SleepSnapshot.Memory
		if name == "disk" {
			status = information.SleepSnapshot.Disk
			if memory := information.SleepSnapshot.Memory; !memory.Uploaded || memory.UploadPercent == nil || *memory.UploadPercent != 100 {
				t.Fatalf("memory completion lost during disk upload: %+v", memory)
			}
		}
		if status.UploadPercent != nil {
			t.Fatal("compression reported HTTP upload progress")
		}
		progress.TotalBytes.Store(128)
		for _, check := range []struct {
			bytes   int64
			percent int
		}{{0, 0}, {64, 50}, {128, 99}} {
			progress.UploadedBytes.Store(check.bytes)
			information, err := manager.Information(t.Context(), "vm-1")
			if err != nil {
				t.Fatal(err)
			}
			status := information.SleepSnapshot.Memory
			if name == "disk" {
				status = information.SleepSnapshot.Disk
			}
			if status.Uploaded || status.UploadPercent == nil || *status.UploadPercent != check.percent || information.Phase != "upload-sleep-"+name {
				t.Fatalf("active %s upload: %+v, phase=%q", name, status, information.Phase)
			}
			data, err := json.Marshal(status)
			if err != nil || (check.percent == 0 && !strings.Contains(string(data), `"upload_percent":0`)) {
				t.Fatalf("zero progress was omitted: %s, %v", data, err)
			}
		}
		for _, file := range []string{"status.json", "sleep-state.json"} {
			data, err := os.ReadFile(filepath.Join(manager.store.virtualMachineDirectory("vm-1"), file))
			if err != nil || strings.Contains(string(data), "upload_percent") || strings.Contains(string(data), "uploaded_bytes") {
				t.Fatalf("progress persisted to %s: %v", file, err)
			}
		}
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	information, err := manager.Information(t.Context(), "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	for _, status := range []*SleepArtifactStatus{information.SleepSnapshot.Memory, information.SleepSnapshot.Disk} {
		if !status.Uploaded || status.UploadPercent == nil || *status.UploadPercent != 100 {
			t.Fatalf("acknowledged upload: %+v", status)
		}
	}
	if len(manager.sleepUploads) != 0 {
		t.Fatal("completed transfer retained live counters")
	}
}

func TestUploadCompletionDoesNotEvictAMigrationSource(t *testing.T) {
	manager, runtime, storage, snapshots := sleepyManager(t)
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	snapshots.duringUpload = func() { lockSource(t, manager, "vm-1") }
	if err := manager.OffloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	information, err := manager.Information(t.Context(), "vm-1")
	if err != nil || information.SleepState != SleepStateSleeping || !runtime.hasSavedState || storage.releases != 0 || snapshots.uploads != 1 {
		t.Fatalf("upload changed a migration source: %+v, %v", information, err)
	}
}
