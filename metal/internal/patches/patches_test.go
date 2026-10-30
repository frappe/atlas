package patches

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
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

func TestRescueMarkerPreservesRecordsAcrossRepeatedStartup(t *testing.T) {
	baseDirectory := t.TempDir()
	names := Names()
	markerIndex := slices.Index(names, "recognize_rescue_state")
	if markerIndex < 0 {
		t.Fatal("rescue compatibility marker is not registered")
	}
	names = names[:markerIndex+1]
	previous, err := json.Marshal(names[:markerIndex])
	if err != nil {
		t.Fatal(err)
	}
	markerPath := filepath.Join(baseDirectory, "patches.json")
	if err := os.WriteFile(markerPath, previous, 0o640); err != nil {
		t.Fatal(err)
	}
	records := map[string]string{
		"config.json": `{"schema_version":1,"id":"vm-1","generation":7,"state":"running"}`,
		"status.json": `{"schema_version":1,"generation":7,"state":"running"}`,
	}
	for name, contents := range records {
		writeMigrationPatchRecord(t, filepath.Join(baseDirectory, "machines", "vm-1", name), contents)
	}
	for range 2 {
		if err := run(all[:markerIndex+1], baseDirectory, slog.New(slog.DiscardHandler)); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(markerPath)
		if err != nil {
			t.Fatal(err)
		}
		var applied []string
		if err := json.Unmarshal(data, &applied); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(applied, names) {
			t.Fatalf("applied patches = %v, want %v", applied, names)
		}
		for name, contents := range records {
			data, err := os.ReadFile(filepath.Join(baseDirectory, "machines", "vm-1", name))
			if err != nil {
				t.Fatal(err)
			}
			if string(data) != contents {
				t.Fatalf("rescue marker changed %s: %s", name, data)
			}
		}
	}
}

func TestInitializeMemoryMigrationFlagsKeepsLegacyCheckpoints(t *testing.T) {
	for _, test := range []struct {
		name     string
		contents string
		mode     any
	}{
		{"source.json", `{"schema_version":2,"id":"mig-1","virtual_machine_id":"vm-1","state":"locked","sequence":7,"acknowledged_sequence":6}`, nil},
		{"destination.json", `{"schema_version":2,"id":"mig-1","virtual_machine_id":"vm-1","state":"copying","intervals":[{"guid":"g1","total_bytes":9007199254740993,"completed":true}]}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			baseDirectory := t.TempDir()
			path := filepath.Join(baseDirectory, "machines", "vm-1", "migration", test.name)
			writeMigrationPatchRecord(t, path, test.contents)
			want := readMigrationPatchRecord(t, path)
			want["has_memory_snapshot"] = test.mode

			for range 2 {
				if err := initializeMemoryMigrationFlags(baseDirectory); err != nil {
					t.Fatal(err)
				}
				if got := readMigrationPatchRecord(t, path); !reflect.DeepEqual(got, want) {
					t.Fatalf("record = %v, want %v", got, want)
				}
			}
		})
	}
}

func TestInitializeMemoryMigrationFlagsPreservesSelectedModes(t *testing.T) {
	for _, name := range []string{"source.json", "destination.json"} {
		for _, mode := range []string{"true", "false", "null"} {
			t.Run(name+"/"+mode, func(t *testing.T) {
				baseDirectory := t.TempDir()
				path := filepath.Join(baseDirectory, "machines", "vm-1", "migration", name)
				contents := fmt.Sprintf(`{"has_memory_snapshot":%s,"memory_snapshot_disk_guid":"g1","memory_transfer":{"sequence":7,"bytes_transferred":4096,"completed":true}}`, mode)
				if name == "source.json" {
					contents = fmt.Sprintf(`{"has_memory_snapshot":%s,"sequence":7,"acknowledged_sequence":6}`, mode)
				}
				writeMigrationPatchRecord(t, path, contents)

				for range 2 {
					if err := initializeMemoryMigrationFlags(baseDirectory); err != nil {
						t.Fatal(err)
					}
					data, err := os.ReadFile(path)
					if err != nil {
						t.Fatal(err)
					}
					if string(data) != contents {
						t.Fatalf("an existing mode or checkpoint changed: %s", data)
					}
				}
			})
		}
	}
}

func TestMemoryMigrationPatchFailureIsNotRecorded(t *testing.T) {
	for _, contents := range []string{"{", "null", "[]"} {
		t.Run(contents, func(t *testing.T) {
			baseDirectory := t.TempDir()
			writeMigrationPatchRecord(t, filepath.Join(baseDirectory, "machines", "vm-1", "migration", "source.json"), `{}`)
			path := filepath.Join(baseDirectory, "machines", "vm-1", "migration", "destination.json")
			writeMigrationPatchRecord(t, path, contents)

			err := Run(baseDirectory, slog.New(slog.DiscardHandler))
			if err == nil || !strings.Contains(err.Error(), path) {
				t.Fatalf("patch error = %v, want the failing record path", err)
			}
			applied, err := os.ReadFile(filepath.Join(baseDirectory, "patches.json"))
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Contains(applied, []byte("initialize_memory_migration_flags")) {
				t.Fatalf("failed patch was marked applied: %s", applied)
			}
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if string(data) != contents {
				t.Fatalf("corrupt record changed: %s", data)
			}
		})
	}
}

func TestMemoryMigrationPatchRetriesAfterPartialUpgrade(t *testing.T) {
	baseDirectory := t.TempDir()
	sourcePath := filepath.Join(baseDirectory, "machines", "vm-1", "migration", "source.json")
	destinationPath := filepath.Join(baseDirectory, "machines", "vm-2", "migration", "destination.json")
	writeMigrationPatchRecord(t, sourcePath, `{"schema_version":2,"sequence":7}`)
	writeMigrationPatchRecord(t, destinationPath, "{")
	logger := slog.New(slog.DiscardHandler)

	if err := Run(baseDirectory, logger); err == nil {
		t.Fatal("a corrupt destination record was accepted")
	}
	wantSource := map[string]any{"schema_version": json.Number("2"), "sequence": json.Number("7"), "has_memory_snapshot": nil}
	if got := readMigrationPatchRecord(t, sourcePath); !reflect.DeepEqual(got, wantSource) {
		t.Fatalf("source was not upgraded before the failure: %v", got)
	}

	writeMigrationPatchRecord(t, destinationPath, `{"schema_version":2}`)
	for range 2 {
		if err := Run(baseDirectory, logger); err != nil {
			t.Fatal(err)
		}
		if mode := readMigrationPatchRecord(t, destinationPath)["has_memory_snapshot"]; mode != false {
			t.Fatalf("destination mode = %v, want false", mode)
		}
	}
}

func TestSleepMigrationPatchPreservesCheckpointsAndRestrictsCredentials(t *testing.T) {
	baseDirectory := t.TempDir()
	for _, name := range []string{"source.json", "destination.json"} {
		path := filepath.Join(baseDirectory, "machines", "vm-1", "migration", name)
		writeMigrationPatchRecord(t, path, `{"schema_version":2,"sequence":7,"intervals":[{"total_bytes":9007199254740993}]}`)
		want := readMigrationPatchRecord(t, path)
		want["schema_version"] = json.Number("3")
		for range 2 {
			if err := initializeSleepMigration(baseDirectory); err != nil {
				t.Fatal(err)
			}
			if got := readMigrationPatchRecord(t, path); !reflect.DeepEqual(got, want) {
				t.Fatalf("migration checkpoint changed: %v", got)
			}
			information, err := os.Stat(path)
			if err != nil || information.Mode().Perm() != 0o600 {
				t.Fatalf("migration credentials are not private: %v, %v", information, err)
			}
		}
	}
}

func TestRenameComatoseSleepStateToComaKeepsOtherValues(t *testing.T) {
	baseDirectory := t.TempDir()
	statusPath := filepath.Join(baseDirectory, "machines", "vm-1", "status.json")
	sourcePath := filepath.Join(baseDirectory, "machines", "vm-1", "migration", "source.json")
	destinationPath := filepath.Join(baseDirectory, "machines", "vm-2", "migration", "destination.json")
	writeMigrationPatchRecord(t, statusPath, `{"schema_version":1,"generation":9007199254740993,"state":"stopped","sleep_state":"comatose","updated_at":"2026-10-23T08:00:00Z"}`)
	writeMigrationPatchRecord(t, sourcePath, `{"schema_version":3,"state":"locked","sleep":{"state":"comatose","checkpoint":{"id":"checkpoint-1"}}}`)
	writeMigrationPatchRecord(t, destinationPath, `{"schema_version":3,"state":"ready","definition":{"sleep":{"state":"comatose"}}}`)

	for range 2 {
		if err := renameComatoseSleepStateToComa(baseDirectory); err != nil {
			t.Fatal(err)
		}
	}

	status := readMigrationPatchRecord(t, statusPath)
	if status["sleep_state"] != "coma" || status["state"] != "stopped" || status["generation"] != json.Number("9007199254740993") {
		t.Fatalf("status = %v", status)
	}
	source := readMigrationPatchRecord(t, sourcePath)
	if source["state"] != "locked" || source["sleep"].(map[string]any)["state"] != "coma" {
		t.Fatalf("source = %v", source)
	}
	destination := readMigrationPatchRecord(t, destinationPath)
	if destination["definition"].(map[string]any)["sleep"].(map[string]any)["state"] != "coma" {
		t.Fatalf("destination = %v", destination)
	}
}

func TestMemoryMigrationPatchRunsWithoutRecords(t *testing.T) {
	if err := Run(t.TempDir(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
}

func TestMoveImageKernelsToKernelStoreSharesOneKernel(t *testing.T) {
	baseDirectory := t.TempDir()
	kernel := strings.Repeat("a", 64)
	for _, image := range []string{"sha256:first", "sha256:second"} {
		writeMigrationPatchRecord(t, filepath.Join(baseDirectory, "images", image, "manifest.json"), `{"kernel_sha256":"`+kernel+`"}`)
		writeMigrationPatchRecord(t, filepath.Join(baseDirectory, "images", image, "vmlinux"), "kernel")
	}

	for range 2 {
		if err := moveImageKernelsToKernelStore(baseDirectory); err != nil {
			t.Fatal(err)
		}
	}

	if data, err := os.ReadFile(filepath.Join(baseDirectory, "kernels", kernel)); err != nil || string(data) != "kernel" {
		t.Fatalf("kernel = %q, error %v", data, err)
	}
	if kernels, _ := filepath.Glob(filepath.Join(baseDirectory, "images", "*", "vmlinux")); len(kernels) != 0 {
		t.Fatalf("image kernels remain: %v", kernels)
	}
}

func writeMigrationPatchRecord(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o640); err != nil {
		t.Fatal(err)
	}
}

func readMigrationPatchRecord(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var record map[string]any
	if err := decoder.Decode(&record); err != nil {
		t.Fatal(err)
	}
	return record
}
