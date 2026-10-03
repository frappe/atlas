package storage

import (
	"context"
	"os"
	"slices"
	"strings"
	"testing"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

func arrangePartialClone(t *testing.T, store *VirtualMachineStore, virtualMachineID string) {
	t.Helper()
	snapshot := store.pool.baseSnapshot("test-image")
	if err := platform.Run(
		context.Background(),
		"zfs",
		"clone",
		snapshot,
		store.pool.virtualMachineDataset(virtualMachineID),
	); err != nil {
		t.Fatalf("arrange clone: %v", err)
	}
}

func destroyLogged(t *testing.T, logFile, virtualMachineID string) bool {
	t.Helper()
	content, err := os.ReadFile(logFile)
	if err != nil {
		if os.IsNotExist(err) {
			return false
		}
		t.Fatal(err)
	}
	return slices.Contains(strings.Split(strings.TrimSpace(string(content)), "\n"), "destroy -r metal/vms/"+virtualMachineID)
}

func TestReleaseFailsWhenContextCancelled(t *testing.T) {
	logFile := fakeZFS(t, "-\n", "none")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()

	err := store.Release(cancelled, "vm-cancelled")
	if err == nil {
		t.Fatal("Release with a cancelled context succeeded, want an error")
	}
	if destroyLogged(t, logFile, "vm-cancelled") {
		t.Error("destroy ran with a cancelled context, want no destroy attempt")
	}
}

func TestReleaseCreatedDiskCleansUpAfterCancel(t *testing.T) {
	logFile := fakeZFS(t, "-\n", "none")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	arrangePartialClone(t, store, "vm-partial")

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	store.releaseCreatedDisk(cancelled, "vm-partial", true)

	if !destroyLogged(t, logFile, "vm-partial") {
		t.Errorf("commands = %q, want a destroy of the partial clone", readCommandLogOrEmpty(t, logFile))
	}
}

func TestReleaseCreatedDiskCleansUpWithLiveContext(t *testing.T) {
	logFile := fakeZFS(t, "-\n", "none")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	arrangePartialClone(t, store, "vm-live")

	store.releaseCreatedDisk(context.Background(), "vm-live", true)

	if !destroyLogged(t, logFile, "vm-live") {
		t.Errorf("commands = %q, want a destroy of the partial clone", readCommandLogOrEmpty(t, logFile))
	}
}

func TestReleaseCreatedDiskKeepsExistingDisk(t *testing.T) {
	for _, testCase := range []struct {
		name string
		ctx  func() context.Context
	}{
		{"live", context.Background},
		{"cancelled", func() context.Context {
			cancelled, cancel := context.WithCancel(context.Background())
			cancel()
			return cancelled
		}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			logFile := fakeZFS(t, "-\n", "none")
			store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}

			store.releaseCreatedDisk(testCase.ctx(), "vm-existing", false)

			if destroyLogged(t, logFile, "vm-existing") {
				t.Error("destroy ran for a disk this call did not create")
			}
		})
	}
}

func readCommandLogOrEmpty(t *testing.T, logFile string) string {
	t.Helper()
	content, err := os.ReadFile(logFile)
	if err != nil {
		return "<no commands ran>"
	}
	return strings.TrimSpace(string(content))
}
