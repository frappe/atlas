package firecracker

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

const rescueVsockPort = 187

// rescueBootRecord identifies one cold boot and its acknowledged reboot intent.
type rescueBootRecord struct {
	ID               string `json:"id"`
	HostBootID       string `json:"host_boot_id"`
	RescueGeneration uint64 `json:"rescue_generation"`
	RebootRequested  bool   `json:"reboot_requested"`
}

// rescueListener owns one guest's socket and serializes intent writes with close.
type rescueListener struct {
	listener *net.UnixListener
	mutex    sync.Mutex
	closed   bool
	record   rescueBootRecord
	path     string
	done     chan struct{}
}

func (configuration Config) rescueBootPath(id string) string {
	return filepath.Join(configuration.vmDir(id), "rescue-boot.json")
}

func hostBootID() (string, error) {
	data, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return "", fmt.Errorf("read host boot identity: %w", err)
	}
	return strings.TrimSpace(string(data)), nil
}

func writeRescueBoot(path string, record rescueBootRecord) error {
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return platform.WriteFile(path, data, 0o600)
}

func (runtime *Runtime) prepareRescueListener(input vm.RuntimeMachine) error {
	hostID, err := hostBootID()
	if err != nil {
		return err
	}
	record := rescueBootRecord{ID: uuid.NewString(), HostBootID: hostID, RescueGeneration: input.RescueGeneration}
	if err := writeRescueBoot(runtime.configuration.rescueBootPath(input.ID), record); err != nil {
		return err
	}
	return runtime.ensureRescueListener(input, record)
}

// inspectRescueBoot ignores notifications from another host boot or session.
func (runtime *Runtime) inspectRescueBoot(input vm.RuntimeMachine, state vm.State) (bool, error) {
	if !input.Specification.Rescue.Enabled {
		return false, nil
	}
	data, err := os.ReadFile(runtime.configuration.rescueBootPath(input.ID))
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var record rescueBootRecord
	if err := json.Unmarshal(data, &record); err != nil {
		return false, fmt.Errorf("read rescue boot identity: %w", err)
	}
	hostID, err := hostBootID()
	if err != nil {
		return false, err
	}
	if record.ID == "" || record.HostBootID != hostID || record.RescueGeneration != input.RescueGeneration {
		return false, nil
	}
	if state == vm.StateRunning || state == vm.StatePaused {
		if err := runtime.ensureRescueListener(input, record); err != nil {
			return false, err
		}
	}
	return record.RebootRequested, nil
}

func (runtime *Runtime) ensureRescueListener(input vm.RuntimeMachine, record rescueBootRecord) error {
	runtime.rescueMutex.Lock()
	defer runtime.rescueMutex.Unlock()
	if runtime.rescueClosed {
		return errors.New("rescue listener is shutting down")
	}
	if current := runtime.rescueListeners[input.ID]; current != nil {
		if current.record.ID == record.ID {
			return nil
		}
		current.close()
		delete(runtime.rescueListeners, input.ID)
	}
	socketPath := filepath.Join(runtime.configuration.chrootRoot(input.ID), fmt.Sprintf("rescue.vsock_%d", rescueVsockPort))
	if err := os.Remove(socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		return err
	}
	if err := os.Chown(socketPath, int(input.UserID), int(input.GroupID)); err != nil {
		_ = listener.Close()
		return err
	}
	if err := os.Chmod(socketPath, 0o600); err != nil {
		_ = listener.Close()
		return err
	}
	boot := &rescueListener{listener: listener, record: record, path: runtime.configuration.rescueBootPath(input.ID), done: make(chan struct{})}
	if runtime.rescueListeners == nil {
		runtime.rescueListeners = make(map[string]*rescueListener)
	}
	runtime.rescueListeners[input.ID] = boot
	go func() {
		defer close(boot.done)
		for {
			connection, err := listener.AcceptUnix()
			if err != nil {
				return
			}
			if err := boot.receive(connection); err != nil {
				runtime.logger.Warn("rescue reboot notification failed", "vm_id", input.ID, "error", err)
			} else {
				runtime.logger.Info("rescue reboot intent accepted", "vm_id", input.ID, "rescue_generation", record.RescueGeneration, "boot_id", record.ID)
			}
			_ = connection.Close()
		}
	}()
	return nil
}

func (boot *rescueListener) receive(connection net.Conn) error {
	if err := connection.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return err
	}
	var request [7]byte
	if _, err := io.ReadFull(connection, request[:]); err != nil {
		return err
	}
	if string(request[:]) != "reboot\n" {
		return errors.New("invalid rescue reboot notification")
	}
	boot.mutex.Lock()
	defer boot.mutex.Unlock()
	if boot.closed {
		return errors.New("rescue boot has ended")
	}
	boot.record.RebootRequested = true
	if err := writeRescueBoot(boot.path, boot.record); err != nil {
		return err
	}
	_, err := io.WriteString(connection, "ok\n")
	return err
}

func (boot *rescueListener) close() {
	boot.mutex.Lock()
	boot.closed = true
	_ = boot.listener.Close()
	boot.mutex.Unlock()
}

// clearRescueBoot runs before a host-directed stop, so its shutdown cannot be
// mistaken for a guest-directed exit from rescue.
func (runtime *Runtime) clearRescueBoot(id string) error {
	runtime.rescueMutex.Lock()
	defer runtime.rescueMutex.Unlock()
	if boot := runtime.rescueListeners[id]; boot != nil {
		boot.close()
		delete(runtime.rescueListeners, id)
	}
	if err := os.Remove(runtime.configuration.rescueBootPath(id)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// Close stops rescue listeners while preserving records and running guests.
func (runtime *Runtime) Close() error {
	runtime.rescueMutex.Lock()
	runtime.rescueClosed = true
	listeners := runtime.rescueListeners
	runtime.rescueListeners = nil
	for _, boot := range listeners {
		boot.close()
	}
	runtime.rescueMutex.Unlock()
	for _, boot := range listeners {
		<-boot.done
	}
	return nil
}
