package firecracker

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/guestagent"
	"github.com/frappe/atlas/metal/internal/vm"
)

const (
	guestUsageTimeout = time.Second
	// guestReplyMaximumBytes bounds each reply, because the guest controls it.
	guestReplyMaximumBytes = 64 << 10
)

// GetGuestUsage asks the guest agent for its usage report. It returns nil when
// the VM has no vsock device or agent, or does not answer.
func (runtime *Runtime) GetGuestUsage(ctx context.Context, input vm.RuntimeMachine) *guestagent.Usage {
	connection, reader, err := runtime.dialGuestAgent(ctx, input.ID, guestagent.UsagePort, guestUsageTimeout)
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

// dialGuestAgent connects to a guest agent port with the Firecracker
// host-initiated vsock handshake: the host sends "CONNECT <port>\n" and
// Firecracker answers "OK <host port>\n". timeout bounds the whole exchange.
func (runtime *Runtime) dialGuestAgent(ctx context.Context, identifier string, port int, timeout time.Duration) (net.Conn, *bufio.Reader, error) {
	dialer := net.Dialer{Timeout: timeout}
	connection, err := dialer.DialContext(ctx, "unix", runtime.configuration.vsockSocketPath(identifier))
	if err != nil {
		return nil, nil, err
	}
	reader := bufio.NewReader(io.LimitReader(connection, guestReplyMaximumBytes))
	err = connection.SetDeadline(time.Now().Add(timeout))
	if err == nil {
		_, err = fmt.Fprintf(connection, "CONNECT %d\n", port)
	}
	if err == nil {
		err = readReply(reader, "OK")
	}
	if err != nil {
		connection.Close()
		return nil, nil, err
	}
	return connection, reader, nil
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
