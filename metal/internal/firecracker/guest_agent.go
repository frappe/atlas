package firecracker

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/frappe/atlas/metal/internal/guestagent"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	// guestHandshakeTimeout bounds the vsock handshake. A guest with a vsock driver
	// answers in under a millisecond. A guest without one never answers.
	guestHandshakeTimeout = 50 * time.Millisecond
	// silentGuestRetryInterval is how long the usage report skips a guest that did not answer the handshake.
	silentGuestRetryInterval = time.Minute

	guestUsageTimeout  = time.Second
	guestFreezeTimeout = 10 * time.Second
	// guestGrowTimeout keeps the reconcile pass short, because it holds the VM operation lock.
	guestGrowTimeout   = 30 * time.Second
	guestRebootTimeout = 2 * time.Second
	// guestReplyMaximumBytes bounds each reply, because the guest controls it.
	guestReplyMaximumBytes = 64 << 10
)

// GetGuestUsage asks the guest agent for its usage report. It returns nil when
// the VM has no vsock device or agent, or does not answer.
func (runtime *Runtime) GetGuestUsage(ctx context.Context, input vm.RuntimeMachine) *guestagent.Usage {
	if runtime.silentGuests.isSkipped(input.ID) {
		return nil
	}
	connection, reader, err := runtime.dialGuestAgent(ctx, input.ID, guestagent.UsagePort, guestUsageTimeout)
	if errors.Is(err, os.ErrDeadlineExceeded) {
		runtime.silentGuests.skip(input.ID)
	}
	if err != nil {
		return nil
	}
	defer connection.Close()

	var usage guestagent.Usage
	if err := json.NewDecoder(reader).Decode(&usage); err != nil {
		return nil
	}
	return &usage
}

// FreezeGuestFilesystems asks the guest agent to freeze the guest file systems.
// thaw closes the connection, and the agent then thaws. Without an agent, thaw
// does nothing and a snapshot is crash-consistent, as without a freeze.
func (runtime *Runtime) FreezeGuestFilesystems(ctx context.Context, input vm.RuntimeMachine) (thaw func()) {
	connection, reader, err := runtime.dialGuestAgent(ctx, input.ID, guestagent.FreezePort, guestFreezeTimeout)
	if err != nil {
		return func() {}
	}
	if err := readReply(reader, "frozen"); err != nil {
		connection.Close()
		runtime.logger.Warn("guest file system freeze failed", "virtual_machine_id", input.ID, "error", err)
		return func() {}
	}

	// The guest stays frozen while the connection is open, so it has no deadline.
	_ = connection.SetDeadline(time.Time{})
	return func() { _ = connection.Close() }
}

// GrowGuestRootFilesystem asks the guest agent to grow the root file system to
// fill a resized disk. Without an agent it does nothing, and the guest grows the
// file system at its next boot.
func (runtime *Runtime) GrowGuestRootFilesystem(ctx context.Context, input vm.RuntimeMachine) error {
	connection, reader, err := runtime.dialGuestAgent(ctx, input.ID, guestagent.GrowRootFilesystemPort, guestGrowTimeout)
	if err != nil {
		return nil
	}
	defer connection.Close()
	return readReply(reader, "ok")
}

// requestGuestReboot asks the guest agent for a clean reboot, which makes
// Firecracker exit. It still works when the guest ignores Ctrl-Alt-Del.
func (runtime *Runtime) requestGuestReboot(ctx context.Context, identifier string) error {
	connection, reader, err := runtime.dialGuestAgent(ctx, identifier, guestagent.RebootPort, guestRebootTimeout)
	if err != nil {
		return err
	}
	defer connection.Close()
	return readReply(reader, "ok")
}

// dialGuestAgent connects to a guest agent port with the Firecracker
// host-initiated vsock handshake: the host sends "CONNECT <port>\n" and
// Firecracker answers "OK <host port>\n". timeout bounds the job after the handshake.
func (runtime *Runtime) dialGuestAgent(ctx context.Context, identifier string, port int, timeout time.Duration) (net.Conn, *bufio.Reader, error) {
	dialer := net.Dialer{Timeout: guestHandshakeTimeout}
	connection, err := dialer.DialContext(ctx, "unix", runtime.configuration.vsockSocketPath(identifier))
	if err != nil {
		return nil, nil, err
	}

	reader := bufio.NewReader(io.LimitReader(connection, guestReplyMaximumBytes))
	err = connection.SetDeadline(time.Now().Add(guestHandshakeTimeout))
	if err == nil {
		_, err = fmt.Fprintf(connection, "CONNECT %d\n", port)
	}
	if err == nil {
		err = readReply(reader, "OK")
	}
	if err == nil {
		err = connection.SetDeadline(time.Now().Add(timeout))
	}
	if err != nil {
		connection.Close()
		return nil, nil, err
	}
	return connection, reader, nil
}

// silentGuests remembers guests that did not answer the vsock handshake, such as an
// image without vsock kernel modules, so that each metrics sample does not wait again.
type silentGuests struct {
	mutex   sync.Mutex
	retryAt map[string]time.Time
}

func (guests *silentGuests) isSkipped(identifier string) bool {
	guests.mutex.Lock()
	defer guests.mutex.Unlock()
	return time.Now().Before(guests.retryAt[identifier])
}

func (guests *silentGuests) skip(identifier string) {
	guests.mutex.Lock()
	defer guests.mutex.Unlock()
	if guests.retryAt == nil {
		guests.retryAt = map[string]time.Time{}
	}
	guests.retryAt[identifier] = time.Now().Add(silentGuestRetryInterval)
}

// forget retries the guest at once, because a new boot can load the vsock driver and a
// pause can make a healthy guest miss one handshake.
func (guests *silentGuests) forget(identifier string) {
	guests.mutex.Lock()
	defer guests.mutex.Unlock()
	delete(guests.retryAt, identifier)
}

// readReply reads one line that starts with want. The agent sends "error <reason>" on failure.
func readReply(reader *bufio.Reader, want string) error {
	line, err := reader.ReadString('\n')
	if err != nil {
		return err
	}

	line = strings.TrimSpace(line)
	if line == want || strings.HasPrefix(line, want+" ") {
		return nil
	}
	if reason, found := strings.CutPrefix(line, "error "); found {
		return errors.New(reason)
	}
	return fmt.Errorf("unexpected guest agent reply %q", line)
}
