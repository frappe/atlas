package firecracker

import (
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
)

func TestRescueIntentSurvivesDaemonRestartButNotSessionOrHostRestart(t *testing.T) {
	directory, err := os.MkdirTemp("", "rescue-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(directory) })
	configuration := Config{MachinesDir: directory, FirecrackerBin: "firecracker"}
	input := vm.RuntimeMachine{ID: "vm-1", UserID: uint32(os.Getuid()), GroupID: uint32(os.Getgid()), RescueGeneration: 1}
	input.Specification.Rescue.Enabled = true
	if err := os.MkdirAll(configuration.chrootRoot(input.ID), 0o700); err != nil {
		t.Fatal(err)
	}
	runtime := &Runtime{configuration: configuration, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	if err := runtime.prepareRescueListener(input); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = runtime.Close() })

	notify := func(message string) {
		t.Helper()
		connection, err := net.Dial("unix", filepath.Join(configuration.chrootRoot(input.ID), "rescue.vsock_187"))
		if err != nil {
			t.Fatal(err)
		}
		defer connection.Close()
		if err := connection.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
			t.Fatal(err)
		}
		if _, err := io.WriteString(connection, message); err != nil {
			t.Fatal(err)
		}
		response, err := io.ReadAll(connection)
		if err != nil {
			t.Fatal(err)
		}
		if message == "reboot\n" && string(response) != "ok\n" {
			t.Fatalf("response = %q", response)
		}
	}
	notify("invalid")
	requested, err := runtime.inspectRescueBoot(input, vm.StateRunning)
	if err != nil || requested {
		t.Fatalf("invalid request accepted: %v %v", requested, err)
	}
	notify("reboot\n")
	notify("reboot\n")
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	runtime = &Runtime{configuration: configuration, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	requested, err = runtime.inspectRescueBoot(input, vm.StateRunning)
	if err != nil || !requested {
		t.Fatalf("daemon restart lost intent: %v %v", requested, err)
	}
	input.RescueGeneration++
	requested, err = runtime.inspectRescueBoot(input, vm.StateStopped)
	if err != nil || requested {
		t.Fatalf("stale session intent: %v %v", requested, err)
	}
	input.RescueGeneration--
	data, err := os.ReadFile(configuration.rescueBootPath(input.ID))
	if err != nil {
		t.Fatal(err)
	}
	var record rescueBootRecord
	if err := json.Unmarshal(data, &record); err != nil {
		t.Fatal(err)
	}
	record.HostBootID = "previous-host-boot"
	if err := writeRescueBoot(configuration.rescueBootPath(input.ID), record); err != nil {
		t.Fatal(err)
	}
	requested, err = runtime.inspectRescueBoot(input, vm.StateStopped)
	if err != nil || requested {
		t.Fatalf("stale host intent: %v %v", requested, err)
	}
	if err := runtime.clearRescueBoot(input.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(configuration.rescueBootPath(input.ID)); !os.IsNotExist(err) {
		t.Fatalf("host stop retained intent: %v", err)
	}
}

func TestRescueBootAndLiveLimitsConfigureBothDrives(t *testing.T) {
	directory, err := os.MkdirTemp("", "rescue-api-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(directory) })
	configuration := Config{MachinesDir: directory, SocketsDir: filepath.Join(directory, "sockets"), FirecrackerBin: "firecracker"}
	input := vm.RuntimeMachine{ID: "vm-1", UserID: uint32(os.Getuid()), GroupID: uint32(os.Getgid()), RescueGeneration: 1}
	input.Specification.Rescue.Enabled = true
	input.Specification.Disk = vm.Disk{ThroughputMiBps: 40, IOPS: 2001}
	if err := os.MkdirAll(filepath.Dir(configuration.chrootSocketPath(input.ID)), 0o700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", configuration.chrootSocketPath(input.ID))
	if err != nil {
		t.Fatal(err)
	}
	var mutex sync.Mutex
	var drives []api.Drive
	var patches []api.PartialDrive
	var vsock api.Vsock
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		mutex.Lock()
		defer mutex.Unlock()
		switch {
		case request.Method == http.MethodGet:
			_, _ = io.WriteString(w, `{"state":"Running"}`)
			return
		case request.URL.Path == "/vsock":
			if err := json.NewDecoder(request.Body).Decode(&vsock); err != nil {
				t.Error(err)
			}
		case strings.HasPrefix(request.URL.Path, "/drives/") && request.Method == http.MethodPut:
			var drive api.Drive
			if err := json.NewDecoder(request.Body).Decode(&drive); err != nil {
				t.Error(err)
			}
			drives = append(drives, drive)
		case strings.HasPrefix(request.URL.Path, "/drives/") && request.Method == http.MethodPatch:
			var drive api.PartialDrive
			if err := json.NewDecoder(request.Body).Decode(&drive); err != nil {
				t.Error(err)
			}
			patches = append(patches, drive)
		}
		w.WriteHeader(http.StatusNoContent)
	})}
	go server.Serve(listener)
	t.Cleanup(func() { _ = server.Close() })
	images := &fakeImages{}
	runtime := NewRuntime(configuration, &stubUnits{active: true}, images, images, &stubSerialBroker{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	t.Cleanup(func() { _ = runtime.Close() })
	if err := runtime.prepareBoot(t.Context(), input); err != nil {
		t.Fatal(err)
	}
	input.Specification.Disk.ThroughputMiBps = 60
	if err := runtime.RefreshDisk(t.Context(), input); err != nil {
		t.Fatal(err)
	}
	mutex.Lock()
	defer mutex.Unlock()
	if len(drives) != 2 || len(patches) != 2 || vsock.GuestCID != 3 || vsock.UDSPath != "/rescue.vsock" {
		t.Fatalf("drives=%v patches=%v vsock=%v", drives, patches, vsock)
	}
	if !drives[0].IsRootDevice || drives[1].IsRootDevice || drives[1].IsReadOnly || drives[1].PathOnHost != "/disk.img" {
		t.Fatalf("boot drives = %+v", drives)
	}
	for index, drive := range patches {
		if drive.DriveID != drives[index].DriveID || drive.PathOnHost != drives[index].PathOnHost || drive.RateLimiter.Bandwidth.Size != 30*1024*1024 || drive.RateLimiter.Ops.Size != 2001 || drive.RateLimiter.Ops.RefillTime != 2000 {
			t.Fatalf("live drive = %+v", drive)
		}
	}
}
