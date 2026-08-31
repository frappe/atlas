package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

// fakeVM / fakeDriver implement the vm interfaces in-memory for handler tests.
type fakeVM struct {
	info    vm.Info
	started bool
	snaps   map[string]bool
}

func (f *fakeVM) ID() string { return f.info.ID }
func (f *fakeVM) Start(context.Context) error {
	f.started = true
	f.info.State = vm.StateRunning
	return nil
}
func (f *fakeVM) Stop(context.Context, bool) error            { f.info.State = vm.StateStopped; return nil }
func (f *fakeVM) Destroy(context.Context) error               { return nil }
func (f *fakeVM) Wait(context.Context) (vm.ExitStatus, error) { return vm.ExitStatus{}, nil }
func (f *fakeVM) Info(context.Context) (vm.Info, error)       { return f.info, nil }
func (f *fakeVM) Snapshot(context.Context, string, vm.SnapshotType) (vm.Snapshot, error) {
	return vm.Snapshot{}, nil
}

func (f *fakeVM) DiskSnapshot(_ context.Context, name string) error {
	if f.snaps == nil {
		f.snaps = map[string]bool{}
	}
	f.snaps[name] = true
	return nil
}
func (f *fakeVM) DiskSnapshots(context.Context) ([]vm.DiskSnapshot, error) {
	out := make([]vm.DiskSnapshot, 0, len(f.snaps))
	for n := range f.snaps {
		out = append(out, vm.DiskSnapshot{Name: n, SizeMiB: 1024, UsedMiB: 8})
	}
	return out, nil
}
func (f *fakeVM) DeleteDiskSnapshot(_ context.Context, name string) error {
	if !f.snaps[name] {
		return vm.ErrNotFound
	}
	delete(f.snaps, name)
	return nil
}
func (f *fakeVM) RestoreDiskSnapshot(_ context.Context, name string) error {
	if f.info.State != vm.StateStopped {
		return vm.ErrConflict
	}
	if !f.snaps[name] {
		return vm.ErrNotFound
	}
	return nil
}

type fakeDriver struct{ vms map[string]*fakeVM }

func (d *fakeDriver) Create(_ context.Context, spec vm.Spec) (vm.VM, error) {
	m := &fakeVM{info: vm.Info{ID: "vm1", State: vm.StateCreated, VCPUs: spec.VCPUs, MemMiB: spec.MemMiB, Image: spec.Image.Name}}
	d.vms[m.info.ID] = m
	return m, nil
}
func (d *fakeDriver) Load(_ context.Context, id string) (vm.VM, error) {
	m, ok := d.vms[id]
	if !ok {
		return nil, vm.ErrNotFound
	}
	return m, nil
}
func (d *fakeDriver) List(context.Context) ([]vm.VM, error) {
	out := make([]vm.VM, 0, len(d.vms))
	for _, m := range d.vms {
		out = append(out, m)
	}
	return out, nil
}
func (d *fakeDriver) Type() vm.DriverType { return "fake" }

func newTestServer() http.Handler {
	return New(&fakeDriver{vms: map[string]*fakeVM{}})
}

func TestCreateBootsAndReturnsVM(t *testing.T) {
	srv := newTestServer()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/vms", strings.NewReader(`{"vcpus":2,"mem_mib":512,"image":"ubuntu","ssh_keys":["ssh-ed25519 AAAA"]}`))
	req.Header.Set("Content-Type", "application/json")
	srv.ServeHTTP(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, body %s", rec.Code, rec.Body)
	}
	var got vmResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.State != "running" { // create boots
		t.Errorf("state = %q, want running", got.State)
	}
	if got.VCPUs != 2 || got.Image != "ubuntu" {
		t.Errorf("resource = %+v", got)
	}
}

func TestGetUnknownIs404(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/vms/nope", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d", rec.Code)
	}
}

func TestResizeNotImplemented(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/vms/vm1/resize", nil))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d", rec.Code)
	}
}

func TestHealth(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
}

// do sends one request and asserts the status code.
func do(t *testing.T, srv http.Handler, method, path, body string, want int) *httptest.ResponseRecorder {
	t.Helper()
	var r io.Reader
	if body != "" {
		r = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, r)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != want {
		t.Fatalf("%s %s = %d, want %d (body %s)", method, path, rec.Code, want, rec.Body)
	}
	return rec
}

func TestSnapshotLifecycle(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu"}`, http.StatusCreated) // vm1, running

	do(t, srv, http.MethodPost, "/vms/vm1/snapshots", `{"name":"snap1"}`, http.StatusCreated)

	rec := do(t, srv, http.MethodGet, "/vms/vm1/snapshots", "", http.StatusOK)
	var listed struct {
		Snapshots []snapResp `json:"snapshots"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &listed); err != nil {
		t.Fatal(err)
	}
	if len(listed.Snapshots) != 1 || listed.Snapshots[0].Name != "snap1" || listed.Snapshots[0].VMID != "vm1" {
		t.Fatalf("snapshots = %+v", listed.Snapshots)
	}

	// Restore while running is a conflict; stop first, then it succeeds.
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/snap1/restore", "", http.StatusConflict)
	do(t, srv, http.MethodPost, "/vms/vm1/stop", `{"force":true}`, http.StatusOK)
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/snap1/restore", "", http.StatusNoContent)

	// Delete once, then again -> 404.
	do(t, srv, http.MethodDelete, "/vms/vm1/snapshots/snap1", "", http.StatusNoContent)
	do(t, srv, http.MethodDelete, "/vms/vm1/snapshots/snap1", "", http.StatusNotFound)
}

func TestCreateSnapshotBadName(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu"}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots", `{"name":"bad/name"}`, http.StatusBadRequest)
}

func TestSnapshotUnknownVMIs404(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms/nope/snapshots", `{"name":"x"}`, http.StatusNotFound)
	do(t, srv, http.MethodGet, "/vms/nope/snapshots", "", http.StatusNotFound)
}
