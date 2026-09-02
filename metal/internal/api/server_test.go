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
	snaps   map[string]bool // snapshot name -> has memory
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
func (f *fakeVM) Pause(context.Context) error                 { f.info.State = vm.StatePaused; return nil }
func (f *fakeVM) Resume(context.Context) error                { f.info.State = vm.StateRunning; return nil }
func (f *fakeVM) Resize(_ context.Context, diskMiB int) error {
	if diskMiB < f.info.DiskMiB {
		return vm.ErrConflict
	}
	f.info.DiskMiB = diskMiB
	return nil
}

func (f *fakeVM) Snapshot(_ context.Context, name string, memory bool) error {
	if f.snaps == nil {
		f.snaps = map[string]bool{}
	}
	f.snaps[name] = memory
	return nil
}
func (f *fakeVM) Snapshots(context.Context) ([]vm.Snapshot, error) {
	out := make([]vm.Snapshot, 0, len(f.snaps))
	for n, mem := range f.snaps {
		out = append(out, vm.Snapshot{Name: n, Memory: mem, SizeMiB: 1024, UsedMiB: 8})
	}
	return out, nil
}
func (f *fakeVM) DeleteSnapshot(_ context.Context, name string) error {
	if _, ok := f.snaps[name]; !ok {
		return vm.ErrNotFound
	}
	delete(f.snaps, name)
	return nil
}
func (f *fakeVM) RestoreSnapshot(_ context.Context, name string) error {
	if _, ok := f.snaps[name]; !ok {
		return vm.ErrNotFound
	}
	f.info.State = vm.StateRunning
	return nil
}
func (f *fakeVM) Promote(_ context.Context, name, _ string) error {
	if !f.snaps[name] { // promote needs a memory snapshot
		return vm.ErrConflict
	}
	return nil
}

type fakeDriver struct {
	vms    map[string]*fakeVM
	images []vm.Image
}

func (d *fakeDriver) Create(_ context.Context, spec vm.Spec) (vm.VM, error) {
	m := &fakeVM{info: vm.Info{ID: "vm1", State: vm.StateCreated, VCPUs: spec.VCPUs, MemMiB: spec.MemMiB, DiskMiB: spec.DiskMiB, Image: spec.Image.Name}}
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
func (d *fakeDriver) Images(context.Context) ([]vm.Image, error) { return d.images, nil }
func (d *fakeDriver) DeleteImage(_ context.Context, ref string) error {
	for i, im := range d.images {
		if im.Ref == ref {
			d.images = append(d.images[:i], d.images[i+1:]...)
			return nil
		}
	}
	return vm.ErrNotFound
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

func TestResizeDiskGrows(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu","disk_mib":1024}`, http.StatusCreated)
	rec := do(t, srv, http.MethodPost, "/vms/vm1/resize", `{"disk_mib":2048}`, http.StatusOK)
	var got vmResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Disk.SizeMiB != 2048 {
		t.Fatalf("disk size = %d, want 2048", got.Disk.SizeMiB)
	}
}

func TestResizeShrinkIs409(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu","disk_mib":2048}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/resize", `{"disk_mib":1024}`, http.StatusConflict)
}

func TestResizeCPUMemNotImplemented(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu"}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/resize", `{"mem_mib":1024}`, http.StatusNotImplemented)
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

	do(t, srv, http.MethodPost, "/vms/vm1/snapshots", `{"name":"snap1","memory":true}`, http.StatusCreated)

	rec := do(t, srv, http.MethodGet, "/vms/vm1/snapshots", "", http.StatusOK)
	var listed struct {
		Snapshots []snapResp `json:"snapshots"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &listed); err != nil {
		t.Fatal(err)
	}
	if len(listed.Snapshots) != 1 || listed.Snapshots[0].Name != "snap1" || !listed.Snapshots[0].Memory {
		t.Fatalf("snapshots = %+v", listed.Snapshots)
	}

	// Restore brings the VM back (metald stops and reloads it internally).
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/snap1/restore", "", http.StatusOK)

	// Delete once, then again -> 404.
	do(t, srv, http.MethodDelete, "/vms/vm1/snapshots/snap1", "", http.StatusNoContent)
	do(t, srv, http.MethodDelete, "/vms/vm1/snapshots/snap1", "", http.StatusNotFound)
}

func TestPauseResume(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu"}`, http.StatusCreated)

	rec := do(t, srv, http.MethodPost, "/vms/vm1/pause", "", http.StatusOK)
	var got vmResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.State != "paused" {
		t.Errorf("state = %q, want paused", got.State)
	}
	rec = do(t, srv, http.MethodPost, "/vms/vm1/resume", "", http.StatusOK)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.State != "running" {
		t.Errorf("state = %q, want running", got.State)
	}
}

func TestPromoteNeedsMemorySnapshot(t *testing.T) {
	srv := newTestServer()
	do(t, srv, http.MethodPost, "/vms", `{"image":"ubuntu"}`, http.StatusCreated)

	// A disk-only snapshot cannot be promoted.
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots", `{"name":"disk"}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/disk/promote", `{"image":"golden"}`, http.StatusConflict)

	// A memory snapshot can, and a bad image ref is rejected.
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots", `{"name":"warm","memory":true}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/warm/promote", `{"image":"golden"}`, http.StatusCreated)
	do(t, srv, http.MethodPost, "/vms/vm1/snapshots/warm/promote", `{"image":"bad/ref"}`, http.StatusBadRequest)
}

func TestImages(t *testing.T) {
	d := &fakeDriver{vms: map[string]*fakeVM{}, images: []vm.Image{{Ref: "golden", Warm: true, SizeMiB: 2048}}}
	srv := New(d)

	rec := do(t, srv, http.MethodGet, "/images", "", http.StatusOK)
	var listed struct {
		Images []imageResp `json:"images"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &listed); err != nil {
		t.Fatal(err)
	}
	if len(listed.Images) != 1 || listed.Images[0].Ref != "golden" || !listed.Images[0].Warm {
		t.Fatalf("images = %+v", listed.Images)
	}
	do(t, srv, http.MethodDelete, "/images/nope", "", http.StatusNotFound)
	do(t, srv, http.MethodDelete, "/images/golden", "", http.StatusNoContent)
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
