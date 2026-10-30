package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/telemetry"
	"github.com/frappe/atlas/metal/internal/vm"
)

type sleepAPIRuntime struct {
	vm.Runtime
	state    vm.RuntimeStatus
	saved    vm.SavedState
	restores int
}

func (runtime *sleepAPIRuntime) Inspect(context.Context, vm.RuntimeMachine) (vm.RuntimeStatus, error) {
	return runtime.state, nil
}

func (runtime *sleepAPIRuntime) SavedState(context.Context, vm.RuntimeMachine) (vm.SavedState, error) {
	return runtime.saved, nil
}

func (runtime *sleepAPIRuntime) DeleteSavedState(context.Context, vm.RuntimeMachine) error {
	runtime.state.HasSavedState = false
	return nil
}

func (runtime *sleepAPIRuntime) InstallSavedState(_ context.Context, _ vm.RuntimeMachine, directory, identity string) error {
	runtime.saved = vm.SavedState{Directory: directory, Identity: identity}
	runtime.state.HasSavedState = true
	return nil
}

func (runtime *sleepAPIRuntime) Restore(context.Context, vm.RuntimeMachine) error {
	runtime.restores++
	runtime.state = vm.RuntimeStatus{State: vm.StateRunning}
	return nil
}

type sleepAPINetwork struct{ vm.Network }

func (sleepAPINetwork) Ensure(context.Context, vm.NetworkRequest) (vm.NetworkInterface, error) {
	return vm.NetworkInterface{}, nil
}

type sleepAPIStorage struct{ vm.Storage }

func (sleepAPIStorage) DiskUsage(context.Context, string) (vm.DiskUsage, error) {
	return vm.DiskUsage{}, nil
}

// Disk hardware stays outside this HTTP and memory-archive integration check.
type sleepAPISnapshots struct {
	*storage.SnapshotStore
	diskRestores int
}

func (snapshots *sleepAPISnapshots) RestoreSleepDisk(context.Context, string, []string, vm.SleepArtifact) error {
	snapshots.diskRestores++
	return nil
}

func TestSleepAPIUploadsAndRestoresWithoutBlockingRequests(t *testing.T) {
	for _, sleeping := range []vm.SleepState{vm.SleepStateDeepSleeping, vm.SleepStateComa} {
		t.Run(string(sleeping), func(t *testing.T) {
			started := make(chan string, 4)
			release := make(chan struct{}, 2)
			object := make(chan []byte, 1)
			objects := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				started <- request.Method
				select {
				case <-release:
				case <-request.Context().Done():
					return
				}
				if request.Method == http.MethodPut {
					data, err := io.ReadAll(request.Body)
					if err != nil {
						t.Error(err)
						return
					}
					object <- data
					writer.Header().Set("ETag", "stored")
				} else {
					if request.URL.Query().Get("get") != "refreshed" {
						t.Error("restore did not use the refreshed download URL")
					}
					writer.Write(<-object)
				}
			}))
			defer objects.Close()
			defer close(release)
			directory := t.TempDir()
			saved := t.TempDir()
			for _, name := range []string{"state", "memory", "metadata.json"} {
				if err := os.WriteFile(filepath.Join(saved, name), []byte("checkpoint contents"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			registry := telemetry.NewRegistry(nil)
			runtime := &sleepAPIRuntime{state: vm.RuntimeStatus{State: vm.StateStopped, HasSavedState: true},
				saved: vm.SavedState{Directory: saved, Identity: "checkpoint"}}
			stores := storage.NewStores(t.Context(), "unused", filepath.Join(t.TempDir(), "images"), nil, registry)
			snapshots := &sleepAPISnapshots{SnapshotStore: stores.Snapshots}
			dependencies := vm.ManagerDependencies{Runtime: runtime, Network: sleepAPINetwork{},
				Storage: sleepAPIStorage{}, Snapshots: snapshots, Telemetry: registry}
			configuration := vm.ManagerConfig{MachinesDirectory: directory}
			manager, err := vm.NewManager(configuration, dependencies)
			if err != nil {
				t.Fatal(err)
			}
			server := httptest.NewServer(newServer(t, manager))
			defer server.Close()
			client := &http.Client{Timeout: time.Second}
			create := strings.Replace(validCreateRequest, `"memory_mib":512`, `"memory_mib":512,"sleep_after_idle_seconds":60`, 1)
			sleepAPIRequest(t, client, server.URL, http.MethodPut, "/v1/vms/vm1", create)
			statusPath := filepath.Join(directory, "vm1", "status.json")
			observed := vm.ObservedRecord{SchemaVersion: 1, Generation: 1, SpecificationGeneration: 1,
				State: vm.StateStopped, SleepState: vm.SleepStateSleeping, UpdatedAt: time.Now().UTC()}
			writeSleepAPIRecord(t, statusPath, observed)
			target := &vm.SleepArtifactTarget{UploadURL: objects.URL + "/memory?put=secret", DownloadURL: objects.URL + "/memory?get=secret"}
			body, err := json.Marshal(vm.SleepStateRequest{Memory: target})
			if err != nil {
				t.Fatal(err)
			}
			sleepAPIRequest(t, client, server.URL, http.MethodPost, "/v1/vms/vm1/sleep-state", string(body))
			finished := make(chan error, 1)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			go func() { finished <- manager.OffloadSleepArtifacts(ctx, "vm1") }()
			waitSleepAPITransfer(t, started, http.MethodPut)
			progress := sleepAPIRequest(t, client, server.URL, http.MethodGet, "/v1/vms/vm1", "")
			percent := progress.Observed.SleepSnapshot.Memory.UploadPercent
			if percent == nil || *percent < 0 || *percent >= 100 || progress.Observed.Phase != "upload-sleep-memory" {
				t.Fatalf("missing live upload progress: %+v", progress.Observed)
			}
			// A retry returns while the object's acknowledgment is still blocked.
			sleepAPIRequest(t, client, server.URL, http.MethodPost, "/v1/vms/vm1/sleep-state", string(body))
			release <- struct{}{}
			waitSleepAPIResult(t, finished)
			uploaded := sleepAPIRequest(t, client, server.URL, http.MethodGet, "/v1/vms/vm1", "")
			if uploaded.Observed.SleepState != string(vm.SleepStateDeepSleeping) || *uploaded.Observed.SleepSnapshot.Memory.UploadPercent != 100 {
				t.Fatalf("upload did not complete: %+v", uploaded.Observed)
			}
			refresh, err := json.Marshal(vm.SleepStateRequest{SnapshotID: uploaded.Observed.SleepSnapshot.ID,
				Memory: &vm.SleepArtifactTarget{ObjectKey: "sleep/source/vm1/memory", DownloadURL: objects.URL + "/memory?get=refreshed"}})
			if err != nil {
				t.Fatal(err)
			}
			refreshed := sleepAPIRequest(t, client, server.URL, http.MethodPatch, "/v1/vms/vm1/sleep-state", string(refresh))
			if refreshed.Observed.SleepSnapshot.Memory.ObjectKey != "sleep/source/vm1/memory" {
				t.Fatal("URL refresh did not persist the original object key")
			}
			select {
			case method := <-started:
				t.Fatalf("URL refresh started a %s transfer", method)
			default:
			}
			if sleeping == vm.SleepStateComa {
				// Seed an already offloaded disk; ZFS publishing has separate storage tests.
				path := filepath.Join(directory, "vm1", "sleep-state.json")
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				var checkpoint map[string]json.RawMessage
				if err := json.Unmarshal(data, &checkpoint); err != nil {
					t.Fatal(err)
				}
				checkpoint["disk"] = checkpoint["memory"]
				writeSleepAPIRecord(t, path, checkpoint)
				observed.SleepState = sleeping
				writeSleepAPIRecord(t, statusPath, observed)
			}
			sleepAPIRequest(t, client, server.URL, http.MethodPut, "/v1/vms/vm1/power", `{"state":"running"}`)
			// An accepted wake must survive a daemon restart and request cancellation.
			restarted, err := vm.NewManager(configuration, dependencies)
			if err != nil {
				t.Fatal(err)
			}
			server = httptest.NewServer(newServer(t, restarted))
			defer server.Close()
			go func() { finished <- restarted.RestoreSleepState(ctx, "vm1") }()
			waitSleepAPITransfer(t, started, http.MethodGet)
			status := sleepAPIRequest(t, client, server.URL, http.MethodGet, "/v1/vms/vm1", "")
			if status.Observed.Phase != "restore-state" || status.Desired.Generation <= status.Observed.Generation {
				t.Fatalf("missing pending wake: %+v", status)
			}
			repeated := sleepAPIRequest(t, client, server.URL, http.MethodPut, "/v1/vms/vm1/power", `{"state":"running"}`)
			if repeated.Desired.Generation != status.Desired.Generation {
				t.Fatal("duplicate start added another wake request")
			}
			// Normal reconciliation skips this VM instead of waiting on the download lock.
			checkContext, cancelCheck := context.WithTimeout(ctx, time.Second)
			defer cancelCheck()
			if err := restarted.Reconcile(checkContext, "vm1"); err != nil {
				t.Fatal(err)
			}
			if err := restarted.RestoreSleepState(checkContext, "vm1"); err != nil {
				t.Fatal(err)
			}
			release <- struct{}{}
			waitSleepAPIResult(t, finished)
			awake := sleepAPIRequest(t, client, server.URL, http.MethodGet, "/v1/vms/vm1", "")
			if awake.Observed.State != "running" || awake.Observed.SleepState != "" || awake.Observed.Error != nil || runtime.restores != 1 {
				t.Fatalf("restore did not complete: %+v; restores=%d", awake.Observed, runtime.restores)
			}
			if sleeping == vm.SleepStateComa && snapshots.diskRestores != 1 {
				t.Fatal("coma restore did not restore the disk")
			}
		})
	}
}

func sleepAPIRequest(t *testing.T, client *http.Client, base, method, path, body string) virtualMachineResponse {
	t.Helper()
	request, err := http.NewRequestWithContext(t.Context(), method, base+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	want := http.StatusAccepted
	if method == http.MethodGet {
		want = http.StatusOK
	}
	if response.StatusCode != want {
		t.Fatalf("%s %s: got %d, want %d", method, path, response.StatusCode, want)
	}
	var information virtualMachineResponse
	if err := json.NewDecoder(response.Body).Decode(&information); err != nil {
		t.Fatal(err)
	}
	return information
}

func writeSleepAPIRecord(t *testing.T, path string, record any) {
	t.Helper()
	data, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func waitSleepAPITransfer(t *testing.T, started <-chan string, method string) {
	t.Helper()
	select {
	case actual := <-started:
		if actual != method {
			t.Fatalf("object storage method = %s, want %s", actual, method)
		}
	case <-time.After(time.Second):
		t.Fatalf("%s transfer did not start", method)
	}
}

func waitSleepAPIResult(t *testing.T, finished <-chan error) {
	t.Helper()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("transfer did not finish")
	}
}
