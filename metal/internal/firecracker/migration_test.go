package firecracker

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestMigrationFingerprintUsesBinaryContents(t *testing.T) {
	first, second := filepath.Join(t.TempDir(), "firecracker"), filepath.Join(t.TempDir(), "firecracker")
	for _, path := range []string{first, second} {
		if err := os.WriteFile(path, []byte("build-one"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chtimes(second, time.Unix(0, 0), time.Unix(0, 0)); err != nil {
		t.Fatal(err)
	}
	fingerprint, err := migrationHostCompatibility(t.Context(), first)
	if err != nil {
		t.Fatal(err)
	}
	other, err := migrationHostCompatibility(t.Context(), second)
	if err != nil || fingerprint != other {
		t.Fatalf("same binary rejected: %v", err)
	}
	if err := os.WriteFile(second, []byte("build-two"), 0o600); err != nil {
		t.Fatal(err)
	}
	other, err = migrationHostCompatibility(t.Context(), second)
	if err != nil || fingerprint == other {
		t.Fatalf("different binary accepted: %v", err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := migrationHostCompatibility(cancelled, first); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestMigrationCPUParserRejectsUnknownHardwareAndPreservesARMIdentity(t *testing.T) {
	info := "CPU implementer: 0x41\nCPU architecture: 8\nCPU part: 0xd0c\nCPU variant: 0x1\nCPU revision: 2\nFeatures: fp asimd aes\n"
	cpu, err := migrationCPUFromInfo(info, "arm64")
	if err != nil {
		t.Fatal(err)
	}
	changed, err := migrationCPUFromInfo(strings.ReplaceAll(info, "revision: 2", "revision: 3"), "arm64")
	if err != nil {
		t.Fatal(err)
	}
	if cpu.Model == changed.Model {
		t.Fatal("ARM CPU revision was not recorded")
	}
	for _, architecture := range []string{"amd64", "arm64", "unsupported"} {
		if _, err := migrationCPUFromInfo("processor: 0", architecture); err == nil {
			t.Fatalf("incomplete %s CPU identity accepted", architecture)
		}
	}
}

type migrationUnits struct {
	stubUnits
	configuration Config
	handler       http.Handler
	testing       *testing.T
}

func (units *migrationUnits) Start(context.Context, string) error {
	path := units.configuration.chrootSocketPath("vm-1")
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: units.handler}
	go server.Serve(listener)
	units.testing.Cleanup(func() { server.Close() })
	units.active = true
	return nil
}

func TestMigrationRestoreLoadsPausedAndRetainsBackingMemory(t *testing.T) {
	for _, failure := range []string{"success", "metadata_failure", "snapshot_failure"} {
		t.Run(failure, func(t *testing.T) {
			root, err := os.MkdirTemp("/tmp", "fc-migration-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { os.RemoveAll(root) })
			configuration := Config{MachinesDir: root, SocketsDir: filepath.Join(root, "sockets"), FirecrackerBin: filepath.Join(root, "binary")}
			if err := os.WriteFile(configuration.FirecrackerBin, []byte("firecracker-build"), 0o600); err != nil {
				t.Fatal(err)
			}
			images := &fakeImages{}
			units := &migrationUnits{configuration: configuration, testing: t}
			runtime := NewRuntime(configuration, units, nil, images, images, &stubSerialBroker{}, nil)
			input := vm.RuntimeMachine{
				ID: "vm-1", UserID: uint32(os.Getuid()), GroupID: uint32(os.Getgid()),
				Specification:           vm.Specification{CPUMillicores: 1000, MemoryMiB: 1, DiskMiB: 4},
				SpecificationGeneration: 2, RestartGeneration: 3,
				NetworkInterface: vm.NetworkInterface{GuestIPAddress: "172.16.0.2", MACAddress: "06:00:ac:10:00:02"},
			}
			compatibility, err := newMigrationSnapshotCompatibility(t.Context(), configuration.FirecrackerBin, input.Specification)
			if err != nil {
				t.Fatal(err)
			}
			compatibility.Host = "different-source-build"
			compatibility.CPU = migrationCPUCompatibility{Vendor: "GenuineIntel", Model: "6/60", Features: "avx2 fma bmi1 bmi2"}
			savedStateFixture(t, configuration, savedStateRequirement{
				VirtualMachineID: input.ID, UserID: input.UserID + 1, SpecificationGeneration: 2, RestartGeneration: 3,
				FirecrackerCompatibility: "source-local-marker", MigrationCompatibility: &compatibility,
			})
			bundle := filepath.Join(configuration.vmDir(input.ID), "incoming")
			if err := os.Rename(configuration.savedStateDirectory(input.ID), bundle); err != nil {
				t.Fatal(err)
			}
			requests := make(chan string, 4)
			units.handler = http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				requests <- request.URL.Path
				if request.URL.Path == "/" {
					json.NewEncoder(writer).Encode(api.InstanceInfo{ID: "vm-1", State: "Paused"})
					return
				}
				if request.URL.Path == "/snapshot/load" {
					var load api.LoadSnapshotRequest
					if err := json.NewDecoder(request.Body).Decode(&load); err != nil {
						t.Error(err)
					}
					if load.Resume || load.Memory.Path != "snap/mem" || load.SnapshotPath != "snap/state" {
						t.Errorf("load = %+v", load)
					}
					if failure == "snapshot_failure" {
						writer.WriteHeader(http.StatusBadRequest)
						json.NewEncoder(writer).Encode(map[string]string{"fault_message": "snapshot CPU not supported"})
						return
					}
				}
				if request.URL.Path == "/drives/drive0" {
					var drive api.PartialDrive
					if err := json.NewDecoder(request.Body).Decode(&drive); err != nil {
						t.Error(err)
					}
					if drive.RateLimiter == nil || drive.RateLimiter.Bandwidth == nil || drive.RateLimiter.Ops == nil || drive.RateLimiter.Bandwidth.Size != 0 || drive.RateLimiter.Ops.Size != 0 {
						t.Errorf("migration throttle was not cleared: %+v", drive.RateLimiter)
					}
				}
				if request.URL.Path == "/mmds" && failure == "metadata_failure" {
					writer.WriteHeader(http.StatusInternalServerError)
					return
				}
				writer.WriteHeader(http.StatusNoContent)
			})
			err = runtime.RestoreMigrationSnapshot(t.Context(), input, bundle)
			if failure == "snapshot_failure" {
				var apiError *api.Error
				if !errors.As(err, &apiError) || apiError.Status != http.StatusBadRequest || apiError.Message != "snapshot CPU not supported" {
					t.Fatalf("Firecracker load error was not preserved: %v", err)
				}
				if len(requests) != 1 || <-requests != "/snapshot/load" {
					t.Fatal("snapshot load failure continued activation")
				}
				return
			}
			if failure == "metadata_failure" {
				if err == nil || !strings.Contains(err.Error(), "refresh snapshot metadata") {
					t.Fatalf("error = %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if len(requests) != 2 || <-requests != "/snapshot/load" || <-requests != "/mmds" {
				t.Fatal("snapshot ran before activation")
			}
			if err := runtime.RefreshDisk(t.Context(), input); err != nil {
				t.Fatal(err)
			}
			if err := os.RemoveAll(bundle); err != nil {
				t.Fatal(err)
			}
			memory, err := os.ReadFile(filepath.Join(configuration.chrootRoot(input.ID), "snap", "mem"))
			if err != nil || len(memory) != 256 {
				t.Fatalf("backing memory lost with staging: %v", err)
			}
		})
	}
}

func TestSavedSnapshotFingerprintsTheRunningJailedBinary(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir(), FirecrackerBin: filepath.Join(t.TempDir(), "firecracker")}
	input := vm.RuntimeMachine{ID: "vm-1", UserID: uint32(os.Getuid()), GroupID: uint32(os.Getgid()), Specification: vm.Specification{CPUMillicores: 1000, MemoryMiB: 1, DiskMiB: 4}}
	if err := os.WriteFile(configuration.FirecrackerBin, []byte("installed-build"), 0o600); err != nil {
		t.Fatal(err)
	}
	jailedBinary := filepath.Join(configuration.chrootRoot(input.ID), "firecracker")
	if err := os.MkdirAll(filepath.Dir(jailedBinary), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(jailedBinary, []byte("running-build"), 0o600); err != nil {
		t.Fatal(err)
	}
	socket := fcSocket(t, func() {
		pending := configuration.pendingSavedStateDirectory(input.ID)
		writeSavedStateFile(t, filepath.Join(pending, "state"), 128)
		writeSavedStateFile(t, filepath.Join(pending, "memory"), 256)
	})
	configuration.SocketsDir = filepath.Dir(socket)
	if err := os.Symlink(socket, configuration.socketPath(input.ID)); err != nil {
		t.Fatal(err)
	}
	runtime := &Runtime{configuration: configuration}
	snapshot, err := runtime.createSavedState(t.Context(), input)
	if err != nil {
		t.Fatal(err)
	}
	captured, err := migrationHostCompatibility(t.Context(), jailedBinary)
	if err != nil {
		t.Fatal(err)
	}
	installed, err := migrationHostCompatibility(t.Context(), configuration.FirecrackerBin)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Metadata.MigrationCompatibility == nil || snapshot.Metadata.MigrationCompatibility.Host != captured.Host || captured.Host == installed.Host {
		t.Fatal("snapshot used the installed binary instead of the running build")
	}
}
