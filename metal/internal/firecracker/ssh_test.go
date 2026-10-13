package firecracker

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/vm"
)

// stubSSHUnits lists fixed session units and records stops. Its Wait fails like
// a wait during shutdown, so no client exit ends a session.
type stubSSHUnits struct {
	sessionIDs []string
	mutex      sync.Mutex
	stopped    []string
}

func (units *stubSSHUnits) Start(context.Context, string, []string, string) error { return nil }

func (units *stubSSHUnits) Stop(_ context.Context, sessionID string) error {
	units.mutex.Lock()
	defer units.mutex.Unlock()
	units.stopped = append(units.stopped, sessionID)
	return nil
}

func (units *stubSSHUnits) Wait(context.Context, string) error {
	return errors.New("stub units do not track ssh clients")
}

func (units *stubSSHUnits) List(context.Context) ([]string, error) { return units.sessionIDs, nil }

// recordingBroker reports a fixed set of open consoles and records closes.
type recordingBroker struct {
	stubSerialBroker
	open   map[string]bool
	mutex  sync.Mutex
	closed []string
}

func (broker *recordingBroker) IsOpen(id string) bool { return broker.open[id] }

func (broker *recordingBroker) Close(id string) error {
	broker.mutex.Lock()
	defer broker.mutex.Unlock()
	broker.closed = append(broker.closed, id)
	return nil
}

func (broker *recordingBroker) Attach(context.Context, string, io.ReadWriter, <-chan console.Winsize) error {
	return nil
}

func writeSSHSessionRecord(t *testing.T, configuration Config, sessionID, virtualMachineID string) {
	t.Helper()
	directory := configuration.sshSessionDir(sessionID)
	if err := writeSSHSessionFiles(directory, filepath.Join(directory, "key"), []byte("key"), sshSessionRecord{
		VirtualMachineID: virtualMachineID,
		KeyIndex:         "console-" + sessionID,
	}); err != nil {
		t.Fatal(err)
	}
}

// A new metald adopts a session whose client still runs on an adopted console,
// cleans up a session whose console is gone, and stops a unit without a record.
func TestRecoverSSHSessionsAdoptsLiveSessionsAndEndsTheRest(t *testing.T) {
	configuration := Config{SocketsDir: t.TempDir()}
	writeSSHSessionRecord(t, configuration, "ssh-live", "vm-1")
	writeSSHSessionRecord(t, configuration, "ssh-gone", "vm-1")
	broker := &recordingBroker{open: map[string]bool{"ssh-live": true}}
	sshUnits := &stubSSHUnits{sessionIDs: []string{"ssh-live", "ssh-orphan"}}
	runtime := NewRuntime(configuration, &stubUnits{}, sshUnits, nil, nil, broker, nil)

	runtime.RecoverSSHSessions()

	if _, err := runtime.ResumeSSH("vm-1", "ssh-live"); err != nil {
		t.Fatalf("resume the live session: %v", err)
	}
	if _, err := runtime.ResumeSSH("vm-2", "ssh-live"); !errors.Is(err, vm.ErrNotFound) {
		t.Fatalf("resume from another VM returned %v, want not found", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for {
		_, statError := os.Stat(configuration.sshSessionDir("ssh-gone"))
		_, resumeError := runtime.ResumeSSH("vm-1", "ssh-gone")
		sshUnits.mutex.Lock()
		stopped := slices.Clone(sshUnits.stopped)
		sshUnits.mutex.Unlock()
		if errors.Is(statError, os.ErrNotExist) && errors.Is(resumeError, vm.ErrNotFound) && slices.Contains(stopped, "ssh-orphan") {
			if slices.Contains(stopped, "ssh-live") {
				t.Fatal("recovery stopped the live session")
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the session without a console was not cleaned up")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if _, err := os.Stat(configuration.sshSessionDir("ssh-live")); err != nil {
		t.Fatalf("the live session lost its files: %v", err)
	}
}

// ssh refuses a private key that other users can read.
func TestSSHSessionKeyIsReadableOnlyByTheOwner(t *testing.T) {
	configuration := Config{SocketsDir: t.TempDir()}
	writeSSHSessionRecord(t, configuration, "ssh-a", "vm-1")

	info, err := os.Stat(filepath.Join(configuration.sshSessionDir("ssh-a"), "key"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("key file mode is %v, want 0600", info.Mode().Perm())
	}
}
