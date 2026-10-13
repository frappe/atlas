package platform

import (
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"syscall"

	"github.com/coreos/go-systemd/v22/activation"
)

// notificationSocketEnvironmentVariable names the systemd notification socket variable.
const notificationSocketEnvironmentVariable = "NOTIFY_SOCKET"

// ErrInvalidStoreName reports a descriptor name that systemd cannot accept.
var ErrInvalidStoreName = errors.New("platform: invalid file descriptor store name")

// FileDescriptorStore keeps service file descriptors in systemd.
type FileDescriptorStore struct {
	notificationSocketAddress string
	storedFiles               map[string][]*os.File
}

// NewFileDescriptorStore reads the systemd notification socket and takes the
// descriptors that systemd passed to this process.
func NewFileDescriptorStore() *FileDescriptorStore {
	return &FileDescriptorStore{
		notificationSocketAddress: os.Getenv(notificationSocketEnvironmentVariable),
		storedFiles:               activation.FilesWithNames(),
	}
}

// IsAvailable reports whether systemd offers a file descriptor store.
func (s *FileDescriptorStore) IsAvailable() bool {
	return s.notificationSocketAddress != ""
}

// Store adds file to the store under name.
func (s *FileDescriptorStore) Store(name string, file *os.File) error {
	if err := validateDescriptorName(name); err != nil {
		return err
	}

	return s.notify("FDSTORE=1\nFDNAME="+name, file)
}

// Remove drops every descriptor that systemd holds under name.
func (s *FileDescriptorStore) Remove(name string) error {
	if err := validateDescriptorName(name); err != nil {
		return err
	}

	return s.notify("FDSTOREREMOVE=1\nFDNAME="+name, nil)
}

// SetStatus sets the status text that systemctl shows for the service.
func (s *FileDescriptorStore) SetStatus(status string) error {
	return s.notify("STATUS="+status, nil)
}

// TakeFiles returns the descriptors from systemd that Listen did not take, keyed by name.
func (s *FileDescriptorStore) TakeFiles() map[string][]*os.File {
	files := s.storedFiles
	s.storedFiles = map[string][]*os.File{}
	return files
}

// Listen returns the stored TCP listener with name when it still listens on
// address. Otherwise it listens on address and stores the new listener. systemd
// keeps a stored socket open across a restart, so the kernel queues new
// connections until the next process accepts them. Call Listen before TakeFiles.
func (s *FileDescriptorStore) Listen(name, address string) (net.Listener, error) {
	storedFiles := s.storedFiles[name]
	delete(s.storedFiles, name)
	if len(storedFiles) > 0 {
		if listener := takeListener(storedFiles, address); listener != nil {
			return listener, nil
		}

		// The address changed or the descriptor is not a listener, so replace it.
		if err := s.Remove(name); err != nil {
			return nil, err
		}
	}

	listener, err := net.Listen("tcp", address)
	if err != nil || !s.IsAvailable() {
		return listener, err
	}

	file, err := listener.(*net.TCPListener).File()
	if err != nil {
		listener.Close()
		return nil, fmt.Errorf("read listener descriptor: %w", err)
	}
	defer file.Close()

	if err := s.Store(name, file); err != nil {
		listener.Close()
		return nil, err
	}

	return listener, nil
}

// takeListener returns the stored listener when it listens on address, or nil.
func takeListener(storedFiles []*os.File, address string) net.Listener {
	defer func() {
		for _, file := range storedFiles {
			file.Close()
		}
	}()

	listener, err := net.FileListener(storedFiles[0])
	if err != nil {
		return nil
	}
	if len(storedFiles) == 1 && isListenAddress(listener.Addr(), address) {
		return listener
	}

	listener.Close()
	return nil
}

// isListenAddress reports whether a listener address matches a configured address.
func isListenAddress(listenerAddress net.Addr, address string) bool {
	configured, err := net.ResolveTCPAddr("tcp", address)
	listening, isTCP := listenerAddress.(*net.TCPAddr)
	if err != nil || !isTCP || listening.Port != configured.Port {
		return false
	}
	if configured.IP == nil {
		return listening.IP.IsUnspecified()
	}

	return listening.IP.Equal(configured.IP)
}

// notify sends one message to the systemd notification socket.
func (s *FileDescriptorStore) notify(notificationMessage string, descriptorFile *os.File) error {
	if !s.IsAvailable() {
		return nil
	}

	notificationSocket, err := syscall.Socket(syscall.AF_UNIX, syscall.SOCK_DGRAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("open notification socket: %w", err)
	}
	defer syscall.Close(notificationSocket)

	notificationAddress := &syscall.SockaddrUnix{
		Name: notificationSocketName(s.notificationSocketAddress),
	}

	if descriptorFile == nil {
		if _, err := syscall.SendmsgN(notificationSocket, []byte(notificationMessage), nil, notificationAddress, 0); err != nil {
			return fmt.Errorf("send notification message: %w", err)
		}

		return nil
	}

	// Control preserves the Go runtime poller registration.
	rawFileDescriptor, err := descriptorFile.SyscallConn()
	if err != nil {
		return fmt.Errorf("read file descriptor: %w", err)
	}

	var notificationError error
	sendDescriptor := func(fileDescriptor uintptr) {
		rights := syscall.UnixRights(int(fileDescriptor))
		_, notificationError = syscall.SendmsgN(
			notificationSocket,
			[]byte(notificationMessage),
			rights,
			notificationAddress,
			0,
		)
	}
	if err := rawFileDescriptor.Control(sendDescriptor); err != nil {
		return fmt.Errorf("access file descriptor: %w", err)
	}
	if notificationError != nil {
		return fmt.Errorf("send notification message: %w", notificationError)
	}

	return nil
}

// notificationSocketName converts the systemd abstract socket notation.
func notificationSocketName(notificationSocketAddress string) string {
	if strings.HasPrefix(notificationSocketAddress, "@") {
		return "\x00" + notificationSocketAddress[1:]
	}

	return notificationSocketAddress
}

// validateDescriptorName rejects an invalid systemd descriptor name.
func validateDescriptorName(name string) error {
	if name == "" || strings.ContainsAny(name, ":\n\x00") {
		return fmt.Errorf("%w: %q", ErrInvalidStoreName, name)
	}

	return nil
}
