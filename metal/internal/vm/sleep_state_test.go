package vm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

type sleepSnapshots struct {
	fakeSnapshots
	uploads        int
	uploadError    error
	duringUpload   func()
	duringStage    func()
	duringProgress func(*SleepUploadProgress)
	downloads      []string
	downloadError  error
	diskPresent    bool
	partCount      int
}

func (snapshots *sleepSnapshots) DownloadSleepMemory(_ context.Context, _ string, sources []string, _ SleepArtifact) error {
	snapshots.downloads = append(snapshots.downloads, "memory:"+strings.Join(sources, ","))
	return snapshots.downloadError
}

func (snapshots *sleepSnapshots) RestoreSleepDisk(_ context.Context, _ string, sources []string, _ SleepArtifact) error {
	if snapshots.diskPresent {
		return nil
	}
	snapshots.downloads = append(snapshots.downloads, "disk:"+strings.Join(sources, ","))
	if snapshots.downloadError != nil {
		return snapshots.downloadError
	}
	snapshots.diskPresent = true
	return nil
}

func (snapshots *sleepSnapshots) StageSleepMemory(context.Context, string, string) error {
	if snapshots.duringStage != nil {
		snapshots.duringStage()
	}
	return nil
}

func (snapshots *sleepSnapshots) StageSleepDisk(ctx context.Context, _ SnapshotRequest, directory string) error {
	return snapshots.StageSleepMemory(ctx, directory, directory)
}

func (snapshots *sleepSnapshots) UploadSleepMemory(_ context.Context, _ string, _ SleepArtifactTarget, progress *SleepUploadProgress) (SleepArtifact, error) {
	snapshots.uploads++
	if snapshots.duringProgress != nil {
		snapshots.duringProgress(progress)
	}
	if snapshots.duringUpload != nil {
		snapshots.duringUpload()
	}
	return SleepArtifact{SizeBytes: 512, StoredSizeBytes: 128, SHA256: strings.Repeat("a", 64), PartCount: snapshots.partCount}, snapshots.uploadError
}

func (snapshots *sleepSnapshots) UploadSleepDisk(ctx context.Context, identifier string, target SleepArtifactTarget, progress *SleepUploadProgress) (SleepArtifact, error) {
	return snapshots.UploadSleepMemory(ctx, identifier, target, progress)
}

func sleepyManager(t *testing.T) (*Manager, *fakeRuntime, *fakeStorage, *sleepSnapshots) {
	t.Helper()
	manager, runtime, _, storage := newTestManager(t)
	manager.traffic = nil
	specification := testSpecification()
	specification.SleepAfterIdleSeconds = 60
	if _, err := manager.Create(t.Context(), "vm-1", specification); err != nil {
		t.Fatal(err)
	}
	desired, err := manager.store.readDesired("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	observed, err := manager.store.readObserved("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.writeAppliedState(desired, &observed, StateStopped, SleepStateSleeping); err != nil {
		t.Fatal(err)
	}
	runtime.hasSavedState = true
	runtime.savedIdentity = "checkpoint-1"
	runtime.savedDirectory = t.TempDir()
	snapshots := &sleepSnapshots{}
	manager.snapshots = snapshots
	return manager, runtime, storage, snapshots
}

func sleepTarget() *SleepArtifactTarget {
	return &SleepArtifactTarget{UploadURL: "https://objects.example/snapshot?put=secret", DownloadURL: "https://objects.example/snapshot?get=secret"}
}

func TestSleepMigrationDefersDownloadsUntilDestinationWake(t *testing.T) {
	for _, state := range []SleepState{SleepStateDeepSleeping, SleepStateComatose} {
		t.Run(string(state), func(t *testing.T) {
			source, sourceRuntime, _, _ := sleepyManager(t)
			sourceRuntime.savedIdentity = strings.Repeat("c", 64)
			memory := sleepTarget()
			memory.ObjectKey = "sleep/source/vm-1/memory"
			request := SleepStateRequest{Memory: memory}
			if state == SleepStateComatose {
				request.Disk = sleepTarget()
				request.Disk.ObjectKey = "sleep/source/vm-1/disk"
			}
			if err := source.OffloadSleepState(t.Context(), "vm-1", request); err != nil {
				t.Fatal(err)
			}
			if err := source.OffloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			if state == SleepStateDeepSleeping {
				record, err := source.readSleepRecord("vm-1")
				if err != nil {
					t.Fatal(err)
				}
				record.Disk = &sleepArtifactRecord{SleepArtifactTarget: *sleepTarget()}
				if err := source.writeSleepRecord("vm-1", record); err != nil {
					t.Fatal(err)
				}
			}
			sleep, err := NewMigrationHost(source).ReadSleepMigration("vm-1")
			if err != nil || sleep == nil || sleep.State != state || sleep.Checkpoint.Memory.UploadURL != "" {
				t.Fatalf("capture = %+v, %v", sleep, err)
			}
			if state == SleepStateDeepSleeping && sleep.Checkpoint.Disk != nil {
				t.Fatal("handoff carried an unfinished source disk upload")
			}
			destination, runtime, _, snapshots := sleepyManager(t)
			runtime.hasSavedState = false
			desired, _ := destination.store.readDesired("vm-1")
			desired.UserID += 100
			if err := destination.store.writeDesired(desired); err != nil {
				t.Fatal(err)
			}
			if err := NewMigrationHost(destination).ApplySleepMigration("vm-1", sleep); err != nil {
				t.Fatal(err)
			}
			guard := lockSource(t, destination, "vm-1")
			if err := destination.SetPowerState(t.Context(), "vm-1", StateRunning); !errors.Is(err, ErrConflict) {
				t.Fatalf("migration accepted a wake request: %v", err)
			}
			if err := reconcileSleeping(t.Context(), destination, "vm-1"); err != nil {
				t.Fatal(err)
			}
			if err := destination.OffloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			if len(snapshots.downloads) != 0 || runtime.restores != 0 || runtime.coldStarts != 0 {
				t.Fatal("handoff downloaded snapshots or started the destination")
			}
			guard.sourceLocked["vm-1"] = false
			if err := wakeTestSleep(t.Context(), destination); err != nil {
				t.Fatal(err)
			}
			wantDownloads := 1
			if state == SleepStateComatose {
				wantDownloads = 2
			}
			if len(snapshots.downloads) != wantDownloads || runtime.restores != 1 || runtime.coldStarts != 0 {
				t.Fatalf("wake did not use imported checkpoint: downloads=%v restores=%d", snapshots.downloads, runtime.restores)
			}
		})
	}
}

// reconcileSleeping runs the pass that owns the VM's sleep state: the normal
// reconciler for local saved state, the restore pool for remote state.
func reconcileSleeping(ctx context.Context, manager *Manager, identifier string) error {
	if err := manager.Reconcile(ctx, identifier); err != nil {
		return err
	}
	return manager.RestoreSleepState(ctx, identifier)
}

func wakeTestSleep(ctx context.Context, manager *Manager) error {
	if err := manager.SetPowerState(ctx, "vm-1", StateRunning); err != nil {
		return err
	}
	return reconcileSleeping(ctx, manager, "vm-1")
}

func TestSleepUploadsEvictUploadedArtifacts(t *testing.T) {
	for _, selected := range []string{"memory", "both"} {
		t.Run(selected, func(t *testing.T) {
			manager, runtime, storage, snapshots := sleepyManager(t)
			request := SleepStateRequest{}
			request.Memory = sleepTarget()
			if selected != "memory" {
				request.Disk = sleepTarget()
			}
			if err := manager.OffloadSleepState(t.Context(), "vm-1", request); err != nil {
				t.Fatal(err)
			}
			if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			information, err := manager.Information(t.Context(), "vm-1")
			if err != nil {
				t.Fatal(err)
			}
			wantState, wantReleases := SleepStateDeepSleeping, 0
			if selected == "both" {
				wantState, wantReleases = SleepStateComatose, 1
			}
			if information.SleepState != wantState || information.State != StateStopped || runtime.hasSavedState || storage.releases != wantReleases || !information.SleepSnapshot.Memory.Uploaded {
				t.Fatalf("uploaded resources were not evicted: %+v; runtime=%+v storage=%+v", information, runtime, storage)
			}
			data, err := json.Marshal(information.SleepSnapshot)
			if err != nil || strings.Contains(string(data), "secret") || strings.Contains(string(data), "objects.example") {
				t.Fatalf("unsafe status: %s (%v)", data, err)
			}
			count := snapshots.uploads
			if err := manager.OffloadSleepState(t.Context(), "vm-1", request); err != nil {
				t.Fatal(err)
			}
			if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil || snapshots.uploads != count {
				t.Fatalf("repeat uploaded again: %v", err)
			}
			file, err := os.Stat(filepath.Join(manager.store.virtualMachineDirectory("vm-1"), "sleep-state.json"))
			if err != nil || file.Mode().Perm() != 0o600 {
				t.Fatalf("sleep credentials permissions: %v, %v", file, err)
			}
		})
	}
}

func TestSleepUploadFailureRetainsLocalState(t *testing.T) {
	manager, runtime, storage, snapshots := sleepyManager(t)
	snapshots.uploadError = errors.New("upload failed")
	snapshots.duringProgress = func(progress *SleepUploadProgress) {
		if progress.TotalBytes.Load() != 0 || progress.UploadedBytes.Load() != 0 {
			t.Fatal("new transfer retained counters from a failed transfer")
		}
		progress.TotalBytes.Store(128)
		progress.UploadedBytes.Store(128)
		information, err := manager.Information(t.Context(), "vm-1")
		if err != nil {
			t.Fatal(err)
		}
		status := information.SleepSnapshot.Memory
		if snapshots.uploads == 3 {
			status = information.SleepSnapshot.Disk
		}
		if status.UploadPercent == nil || *status.UploadPercent != 99 {
			t.Fatalf("unacknowledged body: %+v", status)
		}
	}
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err == nil {
		t.Fatal("upload failure was hidden")
	}
	information, err := manager.Information(t.Context(), "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if !runtime.hasSavedState || storage.releases != 0 || information.Error == nil || information.SleepSnapshot.Memory.Uploaded {
		t.Fatal("upload failure lost local state or was not reported")
	}
	if information.Phase != "upload-sleep-memory" || information.SleepSnapshot.Memory.UploadPercent != nil || len(manager.sleepUploads) != 0 {
		t.Fatal("failed upload retained live progress or lost its phase")
	}
	snapshots.uploadError = nil
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
}

func TestSleepDiskFollowsMemoryUpload(t *testing.T) {
	manager, _, _, snapshots := sleepyManager(t)
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Disk: sleepTarget()}); !errors.Is(err, ErrConflict) {
		t.Fatalf("disk offload before memory = %v", err)
	}
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Disk: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	information, err := manager.Information(t.Context(), "vm-1")
	if err != nil || information.SleepState != SleepStateComatose || snapshots.uploads != 2 {
		t.Fatalf("disk did not follow memory without reuploading: %+v, %v", information.SleepSnapshot, err)
	}
}

func TestWakeDuringUploadDoesNotEvictLiveState(t *testing.T) {
	for _, upload := range []int{1, 2} {
		manager, runtime, storage, snapshots := sleepyManager(t)
		started, finish := make(chan struct{}), make(chan struct{})
		snapshots.duringProgress = func(progress *SleepUploadProgress) {
			progress.TotalBytes.Store(128)
			progress.UploadedBytes.Store(64)
		}
		snapshots.duringUpload = func() {
			if snapshots.uploads == upload {
				close(started)
				<-finish
			}
		}
		if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
			t.Fatal(err)
		}
		result := make(chan error, 1)
		go func() { result <- manager.offloadSleepArtifacts(t.Context(), "vm-1") }()
		<-started
		wakeError := wakeTestSleep(t.Context(), manager)
		information, informationError := manager.Information(t.Context(), "vm-1")
		close(finish)
		if err := <-result; err != nil {
			t.Fatal(err)
		}
		if wakeError != nil || runtime.restores != 1 || runtime.deletes != upload-1 || storage.releases != 0 || snapshots.uploads != upload {
			t.Fatalf("wake raced with eviction: %v, runtime=%+v storage=%+v uploads=%d", wakeError, runtime, storage, snapshots.uploads)
		}
		if informationError != nil || information.Phase != "" {
			t.Fatalf("live upload replaced the completed wake phase: %q, %v", information.Phase, informationError)
		}
	}
}

func TestSleepURLRefreshPersistsWithoutTransferAfterRestart(t *testing.T) {
	manager, runtime, _, snapshots := sleepyManager(t)
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	record, err := manager.readSleepRecord("vm-1")
	if err != nil {
		t.Fatal(err)
	}
	request := SleepStateRequest{SnapshotID: "old-snapshot", Memory: &SleepArtifactTarget{DownloadURL: "https://objects.example/snapshot?get=fresh"}}
	if err := manager.RefreshSleepState(t.Context(), "vm-1", request); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale refresh = %v", err)
	}
	request.SnapshotID = record.ID
	if err := manager.RefreshSleepState(t.Context(), "vm-1", request); err != nil {
		t.Fatal(err)
	}
	restarted, err := NewManager(manager.configuration, ManagerDependencies{Runtime: runtime, Network: manager.network,
		Storage: manager.storage, Snapshots: snapshots, Telemetry: manager.telemetry})
	if err != nil {
		t.Fatal(err)
	}
	saved, err := restarted.readSleepRecord("vm-1")
	if err != nil || saved.Memory.DownloadURL != request.Memory.DownloadURL || saved.Disk.DownloadURL != record.Disk.DownloadURL {
		t.Fatalf("partial refresh did not persist: %v", err)
	}
	if err := restarted.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if snapshots.uploads != 2 || runtime.restores != 0 || runtime.starts != 0 {
		t.Fatal("URL refresh or restart started another transfer or woke the VM")
	}
}

func sleepParts(count int, signature string) *SleepArtifactTarget {
	target := &SleepArtifactTarget{PartSizeBytes: minimumSleepPartSizeBytes}
	for index := range count {
		url := fmt.Sprintf("https://objects.example/memory.%d?", index+1)
		target.Parts = append(target.Parts, SleepArtifactPart{UploadURL: url + "put=" + signature, DownloadURL: url + "get=" + signature})
	}
	return target
}

func TestSleepUploadRemovesStagingOfOlderCheckpoints(t *testing.T) {
	manager, _, _, _ := sleepyManager(t)
	stale := filepath.Join(manager.store.virtualMachineDirectory("vm-1"), "sleep-transfer", "old-checkpoint")
	if err := os.MkdirAll(stale, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget()}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(stale); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stale staging remains: %v", err)
	}
}

func TestSleepPartsRefreshOnlyTheUsedParts(t *testing.T) {
	manager, _, _, snapshots := sleepyManager(t)
	snapshots.partCount = 2
	if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepParts(3, "first")}); err != nil {
		t.Fatal(err)
	}
	if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
		t.Fatal(err)
	}
	record, err := manager.readSleepRecord("vm-1")
	if err != nil || len(record.Memory.Parts) != 2 {
		t.Fatalf("stored parts = %+v, %v; want the 2 used parts", record, err)
	}

	refresh := sleepParts(3, "fresh").withoutUploadURLs()
	if err := manager.RefreshSleepState(t.Context(), "vm-1", SleepStateRequest{SnapshotID: record.ID, Memory: &refresh}); !errors.Is(err, ErrConflict) {
		t.Fatalf("refresh of unused parts = %v", err)
	}
	refresh.Parts = refresh.Parts[:2]
	if err := manager.RefreshSleepState(t.Context(), "vm-1", SleepStateRequest{SnapshotID: record.ID, Memory: &refresh}); err != nil {
		t.Fatal(err)
	}
	if record, err = manager.readSleepRecord("vm-1"); err != nil || !slices.Equal(record.Memory.DownloadURLs(), refresh.DownloadURLs()) {
		t.Fatalf("refreshed parts = %+v, %v", record, err)
	}
}

func TestSleepStateRequestRejectsInvalidTransport(t *testing.T) {
	for _, request := range []SleepStateRequest{
		{},
		{Memory: &SleepArtifactTarget{UploadURL: "file:///disk", DownloadURL: "https://objects.example/memory"}},
		{Memory: &SleepArtifactTarget{UploadURL: "https://objects.example/memory#fragment", DownloadURL: "https://objects.example/memory"}},
		{Memory: &SleepArtifactTarget{UploadURL: "https://user:secret@objects.example/memory", DownloadURL: "https://objects.example/memory"}},
	} {
		if err := request.Validate(false); err == nil {
			t.Fatalf("invalid request accepted: %+v", request)
		}
	}
	if err := (SleepStateRequest{Memory: sleepTarget()}).Validate(true); err == nil {
		t.Fatal("refresh accepted without checkpoint identity")
	}
	if err := (SleepStateRequest{SnapshotID: "checkpoint", Memory: sleepTarget()}).Validate(true); err == nil {
		t.Fatal("refresh accepted an upload destination")
	}
}

func TestSleepRequestsShareOneWakePath(t *testing.T) {
	for _, sleepState := range []SleepState{SleepStateSleeping, SleepStateDeepSleeping, SleepStateComatose} {
		for _, request := range []string{"traffic", "start", "reconcile", "waiting"} {
			t.Run(string(sleepState)+"/"+request, func(t *testing.T) {
				manager, runtime, _, snapshots := sleepyManager(t)
				if sleepState != SleepStateSleeping {
					upload := SleepStateRequest{Memory: sleepTarget()}
					if sleepState == SleepStateComatose {
						upload.Disk = sleepTarget()
					}
					if err := manager.OffloadSleepState(t.Context(), "vm-1", upload); err != nil {
						t.Fatal(err)
					}
					if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
						t.Fatal(err)
					}
				}
				desired, _, err := manager.newVirtualMachine("vm-1").records()
				if err != nil {
					t.Fatal(err)
				}
				snapshots.diskPresent = sleepState != SleepStateComatose
				if request == "waiting" {
					manager.traffic = &traffic.Monitor{}
					// The fixture has no TAP hook. Rearming, not downloading, must fail.
					if err := reconcileSleeping(t.Context(), manager, "vm-1"); !errors.Is(err, traffic.ErrNotFound) {
						t.Fatalf("expected a missing traffic hook, got %v", err)
					}
					information, err := manager.Information(t.Context(), "vm-1")
					if err != nil || information.SleepState != sleepState || runtime.restores != 0 || len(snapshots.downloads) != 0 {
						t.Fatalf("idle VM woke without a request: %+v, %v", information, err)
					}
					return
				}
				if request == "reconcile" {
					err = reconcileSleeping(t.Context(), manager, "vm-1")
				} else {
					manager.traffic = &traffic.Monitor{}
					if request == "traffic" {
						err = manager.RestoreAfterTraffic(t.Context(), traffic.Event{Target: traffic.Target{VirtualMachineID: desired.ID, UserID: desired.UserID}})
					} else {
						err = manager.SetPowerState(t.Context(), "vm-1", StateRunning)
					}
				}
				if err != nil {
					t.Fatal(err)
				}
				if request != "reconcile" {
					if runtime.restores != 0 {
						t.Fatal("wake request restored synchronously")
					}
					if err := reconcileSleeping(t.Context(), manager, "vm-1"); err != nil {
						t.Fatal(err)
					}
				}
				information, err := manager.Information(t.Context(), "vm-1")
				if err != nil || information.SleepState != SleepStateAwake || information.State != StateRunning || runtime.restores != 1 || runtime.starts != 0 {
					t.Fatalf("wake failed: %+v, %v; runtime=%+v", information, err, runtime)
				}
				wantDownloads := 0
				if sleepState == SleepStateDeepSleeping {
					wantDownloads = 1
				} else if sleepState == SleepStateComatose {
					wantDownloads = 2
					if !strings.HasPrefix(snapshots.downloads[0], "disk:") {
						t.Fatal("memory was restored before the disk")
					}
				}
				if len(snapshots.downloads) != wantDownloads {
					t.Fatalf("downloads = %v", snapshots.downloads)
				}
				if request != "reconcile" {
					if err := manager.SetPowerState(t.Context(), "vm-1", StateRunning); err != nil || runtime.restores != 1 {
						t.Fatalf("duplicate request restored again: %v", err)
					}
				}
			})
		}
	}
}

func TestRemoteSleepDownloadFailureCanRetryWithRefreshedURL(t *testing.T) {
	for _, sleepState := range []SleepState{SleepStateDeepSleeping, SleepStateComatose} {
		t.Run(string(sleepState), func(t *testing.T) {
			manager, runtime, _, snapshots := sleepyManager(t)
			if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
				t.Fatal(err)
			}
			if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			desired, observed, err := manager.newVirtualMachine("vm-1").records()
			if err != nil {
				t.Fatal(err)
			}
			if err := manager.writeAppliedState(desired, &observed, StateStopped, sleepState); err != nil {
				t.Fatal(err)
			}
			runtime.hasSavedState = false
			snapshots.downloadError = errors.New("expired URL")
			if err := wakeTestSleep(t.Context(), manager); err == nil {
				t.Fatal("failed download was hidden")
			}
			information, err := manager.Information(t.Context(), "vm-1")
			if err != nil || information.Error == nil || information.SleepState != sleepState || runtime.starts != 0 || runtime.restores != 0 {
				t.Fatalf("failed download changed VM state: %+v, %v", information, err)
			}
			fresh := "https://objects.example/snapshot?get=fresh"
			if err := manager.RefreshSleepState(t.Context(), "vm-1", SleepStateRequest{SnapshotID: information.SleepSnapshot.ID,
				Memory: &SleepArtifactTarget{DownloadURL: fresh}, Disk: &SleepArtifactTarget{DownloadURL: fresh}}); err != nil {
				t.Fatal(err)
			}
			snapshots.downloadError = nil
			if err := wakeTestSleep(t.Context(), manager); err != nil {
				t.Fatal(err)
			}
			if runtime.restores != 1 || !strings.HasSuffix(snapshots.downloads[len(snapshots.downloads)-1], "get=fresh") {
				t.Fatalf("retry did not use refreshed URL: %v", snapshots.downloads)
			}
		})
	}
}

func TestSleepEvictionRetriesWithoutReupload(t *testing.T) {
	for _, artifact := range []string{"memory", "disk"} {
		t.Run(artifact, func(t *testing.T) {
			manager, runtime, storage, snapshots := sleepyManager(t)
			if artifact == "memory" {
				runtime.deleteError = errors.New("delete failed")
			} else {
				storage.releaseError = errors.New("release failed")
			}
			if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
				t.Fatal(err)
			}
			if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err == nil {
				t.Fatal("eviction failure was hidden")
			}
			information, err := manager.Information(t.Context(), "vm-1")
			wantState := SleepStateDeepSleeping
			if artifact == "disk" {
				wantState = SleepStateComatose
			}
			if err != nil || information.Error == nil || information.SleepState != wantState || !information.SleepSnapshot.Memory.Uploaded {
				t.Fatalf("recoverable state was not recorded before eviction: %+v, %v", information, err)
			}
			runtime.deleteError, storage.releaseError = nil, nil
			restarted, err := NewManager(manager.configuration, ManagerDependencies{Runtime: runtime, Network: manager.network,
				Storage: storage, Snapshots: snapshots, Telemetry: manager.telemetry})
			if err != nil {
				t.Fatal(err)
			}
			if err := restarted.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			if runtime.hasSavedState || storage.hasDisk || snapshots.uploads != 2 {
				t.Fatalf("retry did not free resources without reupload: runtime=%+v storage=%+v uploads=%d", runtime, storage, snapshots.uploads)
			}
		})
	}
}

func TestComatosePowerChangesPreserveDisk(t *testing.T) {
	for _, change := range []string{"stop", "restart", "shape"} {
		t.Run(change, func(t *testing.T) {
			manager, runtime, _, snapshots := sleepyManager(t)
			if err := manager.OffloadSleepState(t.Context(), "vm-1", SleepStateRequest{Memory: sleepTarget(), Disk: sleepTarget()}); err != nil {
				t.Fatal(err)
			}
			if err := manager.offloadSleepArtifacts(t.Context(), "vm-1"); err != nil {
				t.Fatal(err)
			}
			var err error
			switch change {
			case "stop":
				err = manager.SetPowerState(t.Context(), "vm-1", StateStopped)
			case "restart":
				err = manager.RequestRestart(t.Context(), "vm-1")
			case "shape":
				err = manager.Resize(t.Context(), "vm-1", Compute{CPUMillicores: 3000, MemoryMiB: 4096}, 4096)
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := reconcileSleeping(t.Context(), manager, "vm-1"); err != nil {
				t.Fatal(err)
			}
			if len(snapshots.downloads) != 1 || !strings.HasPrefix(snapshots.downloads[0], "disk:") || runtime.restores != 0 {
				t.Fatalf("power change did not preserve disk independently of memory: %v, restores=%d", snapshots.downloads, runtime.restores)
			}
		})
	}
}
