package firecracker

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// fakeImages is a storage.Resolver that records disk snapshots and otherwise does
// nothing, so machine snapshot logic can be tested without ZFS.
type fakeImages struct{ snapshotCalls int }

func (f *fakeImages) Prepare(context.Context, storage.Request) (storage.BootConfig, error) {
	return storage.BootConfig{}, nil
}
func (f *fakeImages) PrepareRootfs(context.Context, storage.Request) error { return nil }
func (f *fakeImages) Release(context.Context, string) error                { return nil }
func (f *fakeImages) Resize(context.Context, string, int) error            { return nil }
func (f *fakeImages) Snapshot(context.Context, string, string) error       { f.snapshotCalls++; return nil }
func (f *fakeImages) Snapshots(context.Context, string) ([]storage.SnapshotInfo, error) {
	return nil, nil
}
func (f *fakeImages) DeleteSnapshot(context.Context, string, string) error { return nil }
func (f *fakeImages) Restore(context.Context, string, string) error        { return nil }
func (f *fakeImages) Usage(context.Context, string) (storage.Usage, error) {
	return storage.Usage{}, nil
}
func (f *fakeImages) Promote(context.Context, storage.PromoteRequest) error { return nil }
func (f *fakeImages) ImageMemory(string) (string, string, bool)             { return "", "", false }
func (f *fakeImages) Images(context.Context) ([]storage.ImageInfo, error)   { return nil, nil }
func (f *fakeImages) DeleteImage(context.Context, string) error             { return nil }

// snapSocket serves a firecracker API socket for snapshot tests. It reports the
// guest as Running, records each non-GET request as "<method> <path> [state]",
// and, on snapshot/create, writes the state and mem files firecracker would.
func snapSocket(t *testing.T, chroot string) (string, *[]string) {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "fc.socket")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/" {
			_, _ = w.Write([]byte(`{"state":"Running"}`))
			return
		}
		var body map[string]any
		if b, _ := io.ReadAll(r.Body); len(b) > 0 {
			_ = json.Unmarshal(b, &body)
		}
		label := r.Method + " " + r.URL.Path
		if st, ok := body["state"].(string); ok {
			label += " " + st
		}
		got = append(got, label)
		if r.URL.Path == "/snapshot/create" {
			dir := filepath.Join(chroot, "snap")
			_ = os.MkdirAll(dir, 0o755)
			_ = os.WriteFile(filepath.Join(dir, "state"), []byte("state"), 0o600)
			_ = os.WriteFile(filepath.Join(dir, "mem"), []byte("mem"), 0o600)
		}
		w.WriteHeader(http.StatusNoContent)
	})}
	go srv.Serve(l)
	t.Cleanup(func() { _ = srv.Close() })
	return sock, &got
}

// snapTestMachine builds a running machine backed by a fake socket and fake
// storage, with paths under one tempdir so files move by rename.
func snapTestMachine(t *testing.T) (*machine, *fakeImages, *[]string) {
	t.Helper()
	tmp := t.TempDir()
	// The jail nests in the VM directory, so one base covers both.
	cfg := Config{
		MachinesDir:    filepath.Join(tmp, "machines"),
		FirecrackerBin: "/usr/bin/firecracker",
	}
	id := "abc"
	sock, got := snapSocket(t, cfg.chrootRoot(id))
	imgs := &fakeImages{}
	d := &Driver{cfg: cfg, units: &stubUnits{active: true}, images: imgs}
	m := &machine{
		d:   d,
		cfg: vmConfig{ID: id, UID: uint32(os.Getuid()), GID: uint32(os.Getgid()), Sock: sock, Spec: vm.Spec{MemMiB: 128}},
		api: api.New(sock),
	}
	return m, imgs, got
}

// A disk-only snapshot must not pause the guest or touch the snapshot endpoints.
func TestSnapshotDiskOnlyDoesNotPause(t *testing.T) {
	m, imgs, got := snapTestMachine(t)
	if err := m.Snapshot(context.Background(), "s", false); err != nil {
		t.Fatal(err)
	}
	if imgs.snapshotCalls != 1 {
		t.Errorf("disk snapshots = %d, want 1", imgs.snapshotCalls)
	}
	if len(*got) != 0 {
		t.Errorf("firecracker requests = %v, want none", *got)
	}
}

// A memory snapshot must pause, create, and resume in that order, take the paired
// disk snapshot, and persist the memory files.
func TestSnapshotMemoryPausesCreatesResumes(t *testing.T) {
	m, imgs, got := snapTestMachine(t)
	if err := m.Snapshot(context.Background(), "s", true); err != nil {
		t.Fatal(err)
	}
	want := []string{"PATCH /vm Paused", "PUT /snapshot/create", "PATCH /vm Resumed"}
	if !reflect.DeepEqual(*got, want) {
		t.Errorf("requests = %v, want %v", *got, want)
	}
	if imgs.snapshotCalls != 1 {
		t.Errorf("disk snapshots = %d, want 1", imgs.snapshotCalls)
	}
	if !m.d.cfg.hasSnapMemory("abc", "s") {
		t.Error("memory files were not persisted")
	}
}

// Pause requires a running guest.
func TestPauseStoppedIsConflict(t *testing.T) {
	m, _, _ := snapTestMachine(t)
	m.d.units.(*stubUnits).shutdown() // now inactive -> StateStopped
	if err := m.Pause(context.Background()); err != vm.ErrConflict {
		t.Errorf("pause stopped = %v, want ErrConflict", err)
	}
}
