package api

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"testing"
)

type recordedRequest struct {
	method string
	path   string
	body   map[string]any
}

func recordingSocket(t *testing.T) (*Client, *[]recordedRequest) {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "fc.socket")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	var got []recordedRequest
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if b, _ := io.ReadAll(r.Body); len(b) > 0 {
			_ = json.Unmarshal(b, &body)
		}
		got = append(got, recordedRequest{method: r.Method, path: r.URL.Path, body: body})
		w.WriteHeader(http.StatusNoContent)
	})}
	go srv.Serve(l)
	t.Cleanup(func() { _ = srv.Close() })
	return New(sock), &got
}

// The snapshot lifecycle must hit the exact firecracker endpoints, in order:
// pause, create, load, resume.
func TestSnapshotClientRequests(t *testing.T) {
	cli, got := recordingSocket(t)
	ctx := context.Background()

	if err := cli.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	if err := cli.CreateSnapshot(ctx, CreateSnapshotRequest{
		SnapshotType: "Full",
		SnapshotPath: "snap/state",
		MemoryFile:   "snap/mem",
	}); err != nil {
		t.Fatal(err)
	}
	if err := cli.LoadSnapshot(ctx, LoadSnapshotRequest{
		SnapshotPath: "snap/state",
		Memory:       MemoryBackend{Path: "snap/mem", Type: "File"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := cli.Resume(ctx); err != nil {
		t.Fatal(err)
	}

	want := []struct{ method, path string }{
		{http.MethodPatch, "/vm"},
		{http.MethodPut, "/snapshot/create"},
		{http.MethodPut, "/snapshot/load"},
		{http.MethodPatch, "/vm"},
	}
	if len(*got) != len(want) {
		t.Fatalf("requests = %d, want %d", len(*got), len(want))
	}
	for i, w := range want {
		if (*got)[i].method != w.method || (*got)[i].path != w.path {
			t.Errorf("req %d = %s %s, want %s %s", i, (*got)[i].method, (*got)[i].path, w.method, w.path)
		}
	}
	if (*got)[0].body["state"] != "Paused" {
		t.Errorf("pause state = %v, want Paused", (*got)[0].body["state"])
	}
	if (*got)[3].body["state"] != "Resumed" {
		t.Errorf("resume state = %v, want Resumed", (*got)[3].body["state"])
	}
}
