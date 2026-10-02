package console

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"

	"github.com/creack/pty"
)

// fakeDescriptorStore retains a duplicate of each descriptor for a simulated restart, as systemd does.
type fakeDescriptorStore struct {
	descriptorsByName map[string][]*os.File
}

func newFakeDescriptorStore() *fakeDescriptorStore {
	return &fakeDescriptorStore{descriptorsByName: make(map[string][]*os.File)}
}

func (s *fakeDescriptorStore) Store(name string, file *os.File) error {
	duplicate, err := duplicateDescriptor(file)
	if err != nil {
		return err
	}
	s.descriptorsByName[name] = append(s.descriptorsByName[name], duplicate)
	return nil
}

func (s *fakeDescriptorStore) Remove(name string) error {
	closeFiles(s.descriptorsByName[name])
	delete(s.descriptorsByName, name)
	return nil
}

// TakeFiles duplicates stored descriptors because systemd keeps its own copies.
func (s *fakeDescriptorStore) TakeFiles() map[string][]*os.File {
	passedDescriptors := make(map[string][]*os.File, len(s.descriptorsByName))
	for name, storedFiles := range s.descriptorsByName {
		for _, storedFile := range storedFiles {
			duplicate, err := duplicateDescriptor(storedFile)
			if err != nil {
				panic(err)
			}
			passedDescriptors[name] = append(passedDescriptors[name], duplicate)
		}
	}

	return passedDescriptors
}

// duplicateDescriptor copies a descriptor into an independent file.
func duplicateDescriptor(file *os.File) (*os.File, error) {
	duplicate, err := syscall.Dup(int(file.Fd()))
	if err != nil {
		return nil, err
	}

	return os.NewFile(uintptr(duplicate), file.Name()), nil
}

// expectConsoleOutput waits for console text.
func expectConsoleOutput(t *testing.T, broker *SerialBroker, id string, want string) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client := newFakeClient()
	go func() { _ = broker.Attach(ctx, id, client, make(chan Winsize)) }()
	waitFor(t, func() bool { return bytes.Contains(client.written(), []byte(want)) })
}

// openSlave opens a console PTY slave through its link, as the VM unit does.
func openSlave(t *testing.T, directory string, id string) *os.File {
	t.Helper()
	link, err := os.Readlink(filepath.Join(directory, id))
	if err != nil {
		t.Fatal(err)
	}
	slave, err := os.OpenFile(link, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { slave.Close() })

	return slave
}

// TestShutdownKeepsMastersSoAdoptRestoresRunningConsoles preserves guest output across a restart.
func TestShutdownKeepsMastersSoAdoptRestoresRunningConsoles(t *testing.T) {
	directory := t.TempDir()
	descriptorStore := newFakeDescriptorStore()

	broker := NewSerialBroker(directory, descriptorStore)
	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}

	slave := openSlave(t, directory, "vm-1")

	// The unit holds the slave now, which is when the master can be stored.
	if err := broker.Persist("vm-1"); err != nil {
		t.Fatal(err)
	}

	broker.Shutdown()

	if _, err := slave.Write([]byte("output during restart\r\n")); err != nil {
		t.Fatalf("guest write after shutdown failed, so the master was closed: %v", err)
	}

	restarted := NewSerialBroker(directory, descriptorStore)
	if adopted := restarted.Adopt([]string{"vm-1"}); adopted != 1 {
		t.Fatalf("adopted = %d, want 1", adopted)
	}
	defer restarted.Shutdown()

	expectConsoleOutput(t, restarted, "vm-1", "output during restart")
}

// Open must leave the master out of the store until the unit holds the slave.
func TestOpenStoresNothingUntilPersist(t *testing.T) {
	directory := t.TempDir()
	descriptorStore := newFakeDescriptorStore()

	broker := NewSerialBroker(directory, descriptorStore)
	defer broker.Shutdown()

	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	if stored := len(descriptorStore.descriptorsByName); stored != 0 {
		t.Fatalf("Open stored %d descriptors, want 0", stored)
	}

	if err := broker.Persist("vm-1"); err != nil {
		t.Fatal(err)
	}
	if stored := len(descriptorStore.descriptorsByName["vm-1"]); stored != 1 {
		t.Fatalf("Persist stored %d descriptors, want 1", stored)
	}
}

// Persist reports a VM that has no console.
func TestPersistWithoutAConsoleIsAnError(t *testing.T) {
	broker := NewSerialBroker(t.TempDir(), newFakeDescriptorStore())

	if err := broker.Persist("vm-missing"); !errors.Is(err, ErrConsoleNotFound) {
		t.Fatalf("Persist error = %v, want %v", err, ErrConsoleNotFound)
	}
}

// TestAdoptReleasesConsolesOfGuestsThatAreGone removes stale state.
func TestAdoptReleasesConsolesOfGuestsThatAreGone(t *testing.T) {
	directory := t.TempDir()
	descriptorStore := newFakeDescriptorStore()

	broker := NewSerialBroker(directory, descriptorStore)
	if err := broker.Open("vm-gone"); err != nil {
		t.Fatal(err)
	}
	broker.Shutdown()

	restarted := NewSerialBroker(directory, descriptorStore)
	if adopted := restarted.Adopt(nil); adopted != 0 {
		t.Fatalf("adopted = %d, want 0", adopted)
	}

	if _, err := os.Lstat(filepath.Join(directory, "vm-gone")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stale console link survived adoption: %v", err)
	}
	if err := restarted.Attach(context.Background(), "vm-gone", nil, nil); !errors.Is(err, ErrConsoleNotFound) {
		t.Fatalf("Attach error = %v, want %v", err, ErrConsoleNotFound)
	}
}

// A stored copy lets a console survive more than one metald restart.
func TestAdoptKeepsTheDescriptorForTheNextRestart(t *testing.T) {
	directory := t.TempDir()
	descriptorStore := newFakeDescriptorStore()

	broker := NewSerialBroker(directory, descriptorStore)
	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := broker.Persist("vm-1"); err != nil {
		t.Fatal(err)
	}
	broker.Shutdown()

	for restart := range 3 {
		restarted := NewSerialBroker(directory, descriptorStore)
		if adopted := restarted.Adopt([]string{"vm-1"}); adopted != 1 {
			t.Fatalf("restart %d: adopted = %d, want 1", restart, adopted)
		}
		restarted.Shutdown()

		if count := len(descriptorStore.descriptorsByName["vm-1"]); count != 1 {
			t.Fatalf("restart %d: stored descriptors = %d, want 1", restart, count)
		}
	}
}

// TestAdoptRemovesLinksWithoutAConsole removes a stale link.
func TestAdoptRemovesLinksWithoutAConsole(t *testing.T) {
	directory := t.TempDir()
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(directory, "vm-stale")
	if err := os.Symlink("/dev/pts/7", stale); err != nil {
		t.Fatal(err)
	}

	broker := NewSerialBroker(directory, newFakeDescriptorStore())
	broker.Adopt([]string{"vm-stale"})

	if _, err := os.Lstat(stale); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stale console link survived adoption: %v", err)
	}
}

// TestPersistReplacesAStoredDescriptor keeps one descriptor per VM across a
// stop and a start of the same VM.
func TestPersistReplacesAStoredDescriptor(t *testing.T) {
	directory := t.TempDir()
	descriptorStore := newFakeDescriptorStore()

	broker := NewSerialBroker(directory, descriptorStore)
	defer broker.Shutdown()

	for range 2 {
		if err := broker.Open("vm-1"); err != nil {
			t.Fatal(err)
		}
		if err := broker.Persist("vm-1"); err != nil {
			t.Fatal(err)
		}
		if err := broker.Close("vm-1"); err != nil {
			t.Fatal(err)
		}
	}

	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := broker.Persist("vm-1"); err != nil {
		t.Fatal(err)
	}

	if count := len(descriptorStore.descriptorsByName["vm-1"]); count != 1 {
		t.Fatalf("stored descriptors for vm-1 = %d, want 1", count)
	}
}

// TestCloseRemovesTheStoredDescriptor stops a stopped VM from being adopted.
func TestCloseRemovesTheStoredDescriptor(t *testing.T) {
	descriptorStore := newFakeDescriptorStore()
	broker := NewSerialBroker(t.TempDir(), descriptorStore)

	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := broker.Persist("vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := broker.Close("vm-1"); err != nil {
		t.Fatal(err)
	}

	if _, found := descriptorStore.descriptorsByName["vm-1"]; found {
		t.Fatal("Close left the descriptor in the store")
	}
}

// TestPTYMasterSurvivesOnlyWithAStore checks the PTY lifetime.
func TestPTYMasterSurvivesOnlyWithAStore(t *testing.T) {
	master, slave, err := pty.Open()
	if err != nil {
		t.Fatal(err)
	}
	defer slave.Close()

	if err := master.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := slave.Write([]byte("lost\r\n")); err == nil {
		t.Fatal("write to a slave without a master succeeded, want an error")
	}
}

// A viewer stays attached while the VM restarts and sees the next console.
func TestAttachFollowsTheNextConsoleAfterARestart(t *testing.T) {
	directory := t.TempDir()
	broker := NewSerialBroker(directory, newFakeDescriptorStore())
	defer broker.Shutdown()

	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	firstSlave := openSlave(t, directory, "vm-1")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client := newFakeClient()
	result := make(chan error, 1)
	go func() { result <- broker.Attach(ctx, "vm-1", client, make(chan Winsize)) }()

	if _, err := firstSlave.Write([]byte("before restart\r\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return bytes.Contains(client.written(), []byte("before restart")) })

	if err := broker.Close("vm-1"); err != nil {
		t.Fatal(err)
	}
	if err := broker.Open("vm-1"); err != nil {
		t.Fatal(err)
	}
	secondSlave := openSlave(t, directory, "vm-1")
	if _, err := secondSlave.Write([]byte("after restart\r\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return bytes.Contains(client.written(), []byte("after restart")) })

	cancel()
	if err := <-result; err != nil {
		t.Fatalf("Attach = %v, want nil", err)
	}
}
