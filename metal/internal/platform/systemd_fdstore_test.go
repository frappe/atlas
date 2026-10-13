package platform

import (
	"fmt"
	"net"
	"os"
	"syscall"
	"testing"
)

// newNotificationReceiver returns a systemd notification socket and its address.
func newNotificationReceiver(t *testing.T) (*net.UnixConn, string) {
	t.Helper()

	name := fmt.Sprintf("metald-test-%s-%d", t.Name(), os.Getpid())
	receiver, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: "\x00" + name, Net: "unixgram"})
	if err != nil {
		t.Fatalf("listen on notification socket: %v", err)
	}
	t.Cleanup(func() { receiver.Close() })

	return receiver, "@" + name
}

// readNotification returns the next message and the descriptors it carries.
func readNotification(t *testing.T, receiver *net.UnixConn) (string, []int) {
	t.Helper()

	message := make([]byte, 4096)
	control := make([]byte, 4096)
	messageCount, controlCount, _, _, err := receiver.ReadMsgUnix(message, control)
	if err != nil {
		t.Fatalf("read notification: %v", err)
	}
	if controlCount == 0 {
		return string(message[:messageCount]), nil
	}

	controlMessages, err := syscall.ParseSocketControlMessage(control[:controlCount])
	if err != nil {
		t.Fatalf("parse control message: %v", err)
	}
	descriptors, err := syscall.ParseUnixRights(&controlMessages[0])
	if err != nil {
		t.Fatalf("parse descriptor rights: %v", err)
	}

	return string(message[:messageCount]), descriptors
}

// Store must carry the file itself, not only its name.
func TestStoreSendsTheDescriptor(t *testing.T) {
	receiver, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	payload := newDescriptorFile(t, "carried through the store")
	if err := NewFileDescriptorStore().Store("vm-00010", payload); err != nil {
		t.Fatalf("store descriptor: %v", err)
	}

	message, descriptors := readNotification(t, receiver)
	if message != "FDSTORE=1\nFDNAME=vm-00010" {
		t.Fatalf("unexpected message %q", message)
	}
	if len(descriptors) != 1 {
		t.Fatalf("got %d descriptors, want 1", len(descriptors))
	}

	received := os.NewFile(uintptr(descriptors[0]), "received")
	defer received.Close()
	contents := make([]byte, 64)
	count, _ := received.ReadAt(contents, 0)
	if string(contents[:count]) != "carried through the store" {
		t.Fatalf("descriptor holds %q", contents[:count])
	}
}

func TestRemoveSendsTheName(t *testing.T) {
	receiver, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	if err := NewFileDescriptorStore().Remove("vm-00010"); err != nil {
		t.Fatalf("remove descriptor: %v", err)
	}

	message, descriptors := readNotification(t, receiver)
	if message != "FDSTOREREMOVE=1\nFDNAME=vm-00010" {
		t.Fatalf("unexpected message %q", message)
	}
	if len(descriptors) != 0 {
		t.Fatalf("got %d descriptors, want 0", len(descriptors))
	}
}

// Without NOTIFY_SOCKET, store operations are no-ops.
func TestStoreIsANoOperationWithoutASocket(t *testing.T) {
	t.Setenv(notificationSocketEnvironmentVariable, "")

	store := NewFileDescriptorStore()
	if store.IsAvailable() {
		t.Fatal("store reports available without NOTIFY_SOCKET")
	}
	if err := store.Store("vm-00010", os.Stdin); err != nil {
		t.Fatalf("store descriptor: %v", err)
	}
	if err := store.Remove("vm-00010"); err != nil {
		t.Fatalf("remove descriptor: %v", err)
	}
}

func TestInvalidDescriptorNameIsRejected(t *testing.T) {
	_, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	store := NewFileDescriptorStore()
	for _, name := range []string{"", "vm:00010", "vm\n00010"} {
		if err := store.Store(name, os.Stdin); err == nil {
			t.Fatalf("store accepted name %q", name)
		}
		if err := store.Remove(name); err == nil {
			t.Fatalf("remove accepted name %q", name)
		}
	}
}

// newDescriptorFile returns a temporary file that holds contents.
func newDescriptorFile(t *testing.T, contents string) *os.File {
	t.Helper()

	file, err := os.CreateTemp(t.TempDir(), "descriptor")
	if err != nil {
		t.Fatalf("create temporary file: %v", err)
	}
	t.Cleanup(func() { file.Close() })
	if _, err := file.WriteString(contents); err != nil {
		t.Fatalf("write temporary file: %v", err)
	}

	return file
}

// A new listener goes into the store, so the next process can take it.
func TestListenStoresANewListener(t *testing.T) {
	receiver, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	listener, err := NewFileDescriptorStore().Listen("listener-api", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer listener.Close()

	message, descriptors := readNotification(t, receiver)
	if message != "FDSTORE=1\nFDNAME=listener-api" || len(descriptors) != 1 {
		t.Fatalf("got message %q with %d descriptors", message, len(descriptors))
	}
	syscall.Close(descriptors[0])
}

// A stored listener on the configured address is used again and not stored twice.
func TestListenTakesTheStoredListener(t *testing.T) {
	_, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	stored, storedFile := newStoredListener(t)
	store := &FileDescriptorStore{
		notificationSocketAddress: address,
		storedFiles:               map[string][]*os.File{"listener-api": {storedFile}, "vm-00010": {os.Stdin}},
	}

	listener, err := store.Listen("listener-api", stored.Addr().String())
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer listener.Close()

	if listener.Addr().String() != stored.Addr().String() {
		t.Fatalf("listener uses %s, want %s", listener.Addr(), stored.Addr())
	}
	if files := store.TakeFiles(); len(files) != 1 || files["vm-00010"] == nil {
		t.Fatalf("TakeFiles returned %v, want only the console", files)
	}
}

// A stored listener on another address is removed from the store.
func TestListenReplacesAListenerOnAnotherAddress(t *testing.T) {
	receiver, address := newNotificationReceiver(t)
	t.Setenv(notificationSocketEnvironmentVariable, address)

	_, storedFile := newStoredListener(t)
	store := &FileDescriptorStore{
		notificationSocketAddress: address,
		storedFiles:               map[string][]*os.File{"listener-api": {storedFile}},
	}

	listener, err := store.Listen("listener-api", "127.0.0.2:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer listener.Close()

	if message, _ := readNotification(t, receiver); message != "FDSTOREREMOVE=1\nFDNAME=listener-api" {
		t.Fatalf("first message %q, want the removal", message)
	}
	message, descriptors := readNotification(t, receiver)
	if message != "FDSTORE=1\nFDNAME=listener-api" || len(descriptors) != 1 {
		t.Fatalf("got message %q with %d descriptors", message, len(descriptors))
	}
	syscall.Close(descriptors[0])
}

func newStoredListener(t *testing.T) (net.Listener, *os.File) {
	t.Helper()

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { listener.Close() })

	file, err := listener.(*net.TCPListener).File()
	if err != nil {
		t.Fatalf("read listener descriptor: %v", err)
	}

	return listener, file
}
