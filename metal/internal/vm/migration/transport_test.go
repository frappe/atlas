package migration

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestPrepareSourceReadsTheDescription(t *testing.T) {
	var gotVirtualMachineID, gotPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("sleep_state") != "true" {
			t.Error("destination did not advertise asleep migration support")
		}
		gotVirtualMachineID = r.URL.Query().Get("virtual_machine_id")
		gotPath = r.Method + " " + r.URL.Path
		_, _ = w.Write([]byte(`{"config":{"virtual_machine_id":"vm-1","specification":{"memory_mib":2048}},"observed_state":"running"}`))
	}))
	defer server.Close()

	definition, state, err := NewSourceClient(0, nil).PrepareSource(t.Context(), server.URL, "mig-1", "vm-1")
	if err != nil {
		t.Fatal(err)
	}
	if gotVirtualMachineID != "vm-1" || gotPath != "PUT /v1/migrations/mig-1/source" {
		t.Fatalf("path = %q vm = %q", gotPath, gotVirtualMachineID)
	}
	if definition.VirtualMachineID != "vm-1" || definition.Specification.MemoryMiB != 2048 || state != vm.StateRunning {
		t.Fatalf("definition = %+v state = %s", definition, state)
	}
}

func TestSourceSnapshotAcknowledgements(t *testing.T) {
	var paths, bodies []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		paths = append(paths, r.Method+" "+r.URL.Path)
		bodies = append(bodies, string(body))
		if strings.HasSuffix(r.URL.Path, "/snapshot") {
			_, _ = w.Write([]byte(`{"sequence":2,"size_bytes":1048576,"guid":"g2"}`))
			return
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	client := NewSourceClient(0, nil)

	snapshot, err := client.NextSnapshot(t.Context(), server.URL, "mig-1", "vm-1", 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := client.StartSnapshotStream(t.Context(), server.URL, "mig-1", "vm-1", 2, "resume", 32); err != nil {
		t.Fatal(err)
	}
	if snapshot.Sequence != 2 || snapshot.SizeBytes != 1048576 || snapshot.GUID != "g2" {
		t.Fatalf("snapshot = %+v", snapshot)
	}
	if paths[0] != "POST /v1/migrations/mig-1/snapshot" || !strings.Contains(bodies[0], `"received_sequence":1`) {
		t.Fatalf("snapshot request = %s %s", paths[0], bodies[0])
	}
	if paths[1] != "POST /v1/migrations/mig-1/stream" || !strings.Contains(bodies[1], `"resume_token":"resume"`) {
		t.Fatalf("stream request = %s %s", paths[1], bodies[1])
	}
}

func TestStopSourceReturnsFinalSnapshot(t *testing.T) {
	var request SnapshotAcknowledgement
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_, _ = w.Write([]byte(`{"sequence":3,"size_bytes":2048,"guid":"final"}`))
	}))
	defer server.Close()

	snapshot, err := NewSourceClient(0, nil).StopSource(t.Context(), server.URL, "mig-1", "vm-1", 2, false)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Sequence != 3 || request.ReceivedSequence != 2 {
		t.Fatalf("snapshot = %+v received = %d", snapshot, request.ReceivedSequence)
	}
}

func TestStopSourceReportsASourceWithoutMemory(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
	}))
	defer server.Close()

	if _, err := NewSourceClient(0, nil).StopSource(t.Context(), server.URL, "mig-1", "vm-1", 2, true); !errors.Is(err, ErrNoMemoryToSave) {
		t.Fatalf("stop error = %v", err)
	}
}

func TestStopSourceReportsAWokenSource(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"error":{"code":"source_woke","message":"sleeping source received traffic"}}`))
	}))
	defer server.Close()

	if _, err := NewSourceClient(0, nil).StopSource(t.Context(), server.URL, "mig-1", "vm-1", 2, false); !errors.Is(err, ErrSourceWoke) {
		t.Fatalf("stop error = %v", err)
	}
}

func TestSourceClientUsesALongerDefaultStopTimeout(t *testing.T) {
	client := NewSourceClient(0, nil)
	if client.client.Timeout != defaultControlTimeout || client.stopClient.Timeout != defaultStopTimeout {
		t.Fatalf("timeouts = %s and %s", client.client.Timeout, client.stopClient.Timeout)
	}
}

func TestSourceLifecyclePaths(t *testing.T) {
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := NewSourceClient(0, nil)

	if err := client.StartSource(context.Background(), server.URL, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := client.FinishSource(context.Background(), server.URL, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := client.RemoveSource(context.Background(), server.URL, "mig-1", "vm-1"); err != nil {
		t.Fatal(err)
	}
	want := []string{"POST /v1/migrations/mig-1/start", "POST /v1/migrations/mig-1/destroy", "DELETE /v1/migrations/mig-1"}
	if strings.Join(paths, "|") != strings.Join(want, "|") {
		t.Fatalf("paths = %v", paths)
	}
}

func TestSourceClientReportsErrors(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/migrations/gone/source" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	client := NewSourceClient(0, nil)

	if _, _, err := client.PrepareSource(t.Context(), server.URL, "gone", "vm-1"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("missing source = %v", err)
	}
	if _, _, err := client.PrepareSource(t.Context(), server.URL, "mig-1", "vm-1"); err == nil {
		t.Fatal("want an error for HTTP 500")
	}
}
