package firecracker

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestCPUProfileCompatibility(t *testing.T) {
	profiles := make(map[string]bool)
	for _, name := range []string{"intel-x86_64", "amd-x86_64", "arm64"} {
		content, err := cpuTemplates.ReadFile("cpu_templates/" + name + "-v1.json")
		if err != nil {
			t.Fatal(err)
		}
		service := &Runtime{configuration: Config{CPUTemplate: content}}
		profile, err := service.cpuProfile()
		if err != nil || !strings.HasPrefix(profile, "custom:") {
			t.Fatalf("%s profile = %q, error = %v", name, profile, err)
		}
		if profiles[profile] {
			t.Fatal("different built-in templates share a profile digest")
		}
		profiles[profile] = true
	}
	first := &Runtime{configuration: Config{CPUTemplate: json.RawMessage(`{"a":1,"b":2}`)}}
	second := &Runtime{configuration: Config{CPUTemplate: json.RawMessage(`{ "b":2, "a":1 }`)}}
	left, err := first.cpuProfile()
	if err != nil {
		t.Fatal(err)
	}
	right, err := second.cpuProfile()
	if err != nil || left != right || !strings.HasPrefix(left, "custom:") {
		t.Fatalf("equivalent custom profiles differ: %q, %q, %v", left, right, err)
	}
	second.configuration.CPUTemplate = json.RawMessage(`{"a":2,"b":2}`)
	right, err = second.cpuProfile()
	if err != nil || left == right {
		t.Fatal("different custom profiles match")
	}
	profile, err := (&Runtime{}).cpuProfile()
	if err != nil || profile != "native" {
		t.Fatalf("native profile = %q, error = %v", profile, err)
	}
}

func TestHostCompatibilityProbe(t *testing.T) {
	if runtime.GOARCH == "amd64" {
		kvm, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0)
		if err != nil {
			t.Skipf("KVM is unavailable: %v", err)
		}
		kvm.Close()
	}
	configuration := Config{FirecrackerBin: filepath.Join(t.TempDir(), "firecracker")}
	if err := os.WriteFile(configuration.FirecrackerBin, []byte("test executable"), 0o600); err != nil {
		t.Fatal(err)
	}
	service := &Runtime{configuration: configuration}
	report, err := service.HostCompatibility(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if report.Architecture != runtime.GOARCH || report.CPUProfile != "native" || report.SnapshotHost == "" || report.CPUFeatures == "" {
		t.Fatalf("incomplete compatibility report: %+v", report)
	}
	if runtime.GOARCH == "amd64" && report.TSCKHz <= 0 {
		t.Fatal("KVM did not report a CPU clock")
	}
}

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
	fingerprint, err := hostCompatibility(t.Context(), first)
	if err != nil {
		t.Fatal(err)
	}
	other, err := hostCompatibility(t.Context(), second)
	if err != nil || fingerprint.SnapshotHost != other.SnapshotHost {
		t.Fatalf("same binary rejected: %v", err)
	}
	if err := os.WriteFile(second, []byte("build-two"), 0o600); err != nil {
		t.Fatal(err)
	}
	other, err = hostCompatibility(t.Context(), second)
	if err != nil || fingerprint.SnapshotHost == other.SnapshotHost {
		t.Fatalf("different binary accepted: %v", err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := hostCompatibility(cancelled, first); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestMigrationCPUParserRejectsUnknownHardwareAndPreservesARMIdentity(t *testing.T) {
	info := "CPU implementer: 0x41\nCPU architecture: 8\nCPU part: 0xd0c\nCPU variant: 0x1\nCPU revision: 2\nFeatures: fp asimd aes\n"
	cpu, err := parseHostCPU(info, "arm64")
	if err != nil {
		t.Fatal(err)
	}
	changed, err := parseHostCPU(strings.ReplaceAll(info, "revision: 2", "revision: 3"), "arm64")
	if err != nil {
		t.Fatal(err)
	}
	if cpu.Model == changed.Model {
		t.Fatal("ARM CPU revision was not recorded")
	}
	for _, architecture := range []string{"amd64", "arm64", "unsupported"} {
		if _, err := parseHostCPU("processor: 0", architecture); err == nil {
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
	for _, failure := range []string{"success", "metadata_failure", "snapshot_failure", "tap_failure"} {
		t.Run(failure, func(t *testing.T) {
			root, err := os.MkdirTemp("/tmp", "fc-migration-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { os.RemoveAll(root) })
			commandFile := filepath.Join(root, "tap-command")
			if err := os.WriteFile(filepath.Join(root, "ip"), []byte("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$TAP_COMMAND_FILE\"\n[ \"$TAP_RESTORE_FAIL\" != 1 ]\n"), 0o755); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", root+":"+os.Getenv("PATH"))
			t.Setenv("TAP_COMMAND_FILE", commandFile)
			t.Setenv("TAP_RESTORE_FAIL", "0")
			if failure == "tap_failure" {
				t.Setenv("TAP_RESTORE_FAIL", "1")
			}
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
				NetworkInterface: vm.NetworkInterface{NetworkNamespacePath: "/run/netns/destination-vm", TapName: "tap0", GuestIPAddress: "172.16.0.2", MACAddress: "06:00:ac:10:00:02"},
			}
			shape := vm.MemorySnapshotConfiguration{VirtualCPUCount: 1, MemoryMiB: 1, DiskMiB: 4}
			savedStateFixture(t, configuration, savedStateRequirement{
				VirtualMachineID: input.ID, UserID: input.UserID + 1, SpecificationGeneration: 2, RestartGeneration: 3,
				FirecrackerCompatibility: "source-local-marker", MigratedShape: &shape,
			})
			incoming := filepath.Join(configuration.vmDir(input.ID), "incoming")
			if err := os.Rename(configuration.savedStateDirectory(input.ID), incoming); err != nil {
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
					command, err := os.ReadFile(commandFile)
					if err != nil || strings.TrimSpace(string(command)) != "netns exec destination-vm ip link set dev tap0 address 02:11:22:33:44:55" {
						t.Errorf("source gateway MAC was not applied before snapshot load: %q, %v", command, err)
					}
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
			err = runtime.RestorePausedFrom(t.Context(), input, incoming)
			if failure == "tap_failure" {
				if err == nil || !strings.Contains(err.Error(), "restore snapshot TAP MAC") || units.active || len(requests) != 0 {
					t.Fatalf("TAP failure started the guest: %v", err)
				}
				return
			}
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
			if err := os.RemoveAll(incoming); err != nil {
				t.Fatal(err)
			}
			memory, err := os.ReadFile(filepath.Join(configuration.chrootRoot(input.ID), "snap", "mem"))
			if err != nil || len(memory) != 256 {
				t.Fatalf("backing memory lost with staging: %v", err)
			}
		})
	}
}

func TestSavedSnapshotCapturesShapeAndGatewayMACWithoutHostProbes(t *testing.T) {
	configuration := Config{MachinesDir: t.TempDir(), FirecrackerBin: "/no/such/firecracker"}
	input := vm.RuntimeMachine{ID: "vm-1", UserID: uint32(os.Getuid()), GroupID: uint32(os.Getgid()), Specification: vm.Specification{CPUMillicores: 1000, MemoryMiB: 1, DiskMiB: 4}}
	input.NetworkInterface = vm.NetworkInterface{NetworkNamespacePath: "/run/netns/source-vm", TapName: "tap0"}
	commands := t.TempDir()
	if err := os.WriteFile(filepath.Join(commands, "ip"), []byte("#!/bin/sh\n[ \"$*\" = 'netns exec source-vm cat /sys/class/net/tap0/address' ] || exit 1\nprintf '02:11:22:33:44:55\\n'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", commands+":"+os.Getenv("PATH"))
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
	shape := snapshot.Metadata.Shape
	if shape == nil || *shape != (vm.MemorySnapshotConfiguration{VirtualCPUCount: 1, MemoryMiB: 1, DiskMiB: 4}) {
		t.Fatalf("snapshot shape = %+v", shape)
	}
	if snapshot.Metadata.TapMACAddress != "02:11:22:33:44:55" {
		t.Fatalf("snapshot lost the source TAP MAC: %q", snapshot.Metadata.TapMACAddress)
	}
}
