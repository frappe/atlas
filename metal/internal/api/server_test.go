package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/frappe/atlas-neo/metal/internal/vm"
)

// fakeVM / fakeDriver implement the vm interfaces in-memory for handler tests.
type fakeVM struct {
	info    vm.Info
	started bool
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
