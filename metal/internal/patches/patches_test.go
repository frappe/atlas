package patches

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestRenameStatusKeysToSnakeCaseKeepsTheValues(t *testing.T) {
	baseDirectory := t.TempDir()
	statusPath := filepath.Join(baseDirectory, "machines", "vm-1", "status.json")
	idlePath := filepath.Join(baseDirectory, "machines", "vm-2", "status.json")
	for path, contents := range map[string]string{
		statusPath: `{"schema_version":1,"state":"running","updated_at":"2026-10-13T08:00:00Z",
			"network_interface":{"NetworkNamespacePath":"/run/netns/metal-vm-1","TapName":"tap0","MACAddress":"02:fc:00:00:00:01","GuestIPAddress":"172.16.0.2","GatewayIPAddress":"172.16.0.1"},
			"disk":{"SizeMiB":20480,"UsedMiB":5120}}`,
		idlePath: `{"schema_version":1,"state":"stopped","updated_at":"2026-10-13T08:00:00Z"}`,
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), 0o640); err != nil {
			t.Fatal(err)
		}
	}

	// A second run must leave the same state, because metald can stop before it lists the patch.
	for range 2 {
		if err := renameStatusKeysToSnakeCase(baseDirectory); err != nil {
			t.Fatal(err)
		}
	}

	// metald rejects unknown fields, so every old key must be gone.
	data, err := os.ReadFile(statusPath)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var record vm.ObservedRecord
	if err := decoder.Decode(&record); err != nil {
		t.Fatal(err)
	}
	if record.NetworkInterface.TapName != "tap0" || record.NetworkInterface.GuestIPAddress != "172.16.0.2" ||
		record.Disk.SizeMiB != 20480 || record.Disk.UsedMiB != 5120 {
		t.Fatalf("network interface = %+v, disk = %+v", record.NetworkInterface, record.Disk)
	}

	idle, err := os.ReadFile(idlePath)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(idle, []byte("network_interface")) || bytes.Contains(idle, []byte("disk")) {
		t.Fatalf("a record without network or disk state gained keys: %s", idle)
	}
}

func TestRunAppliesEachPatchOnce(t *testing.T) {
	runs := 0
	list := []Patch{{Name: "count_runs", Apply: func(string) error { runs++; return nil }}}

	baseDirectory := t.TempDir()
	for range 2 {
		if err := run(list, baseDirectory, slog.New(slog.DiscardHandler)); err != nil {
			t.Fatal(err)
		}
	}

	if runs != 1 {
		t.Fatalf("the patch ran %d times, want once", runs)
	}
}
