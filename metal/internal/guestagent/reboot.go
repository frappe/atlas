package guestagent

import (
	"context"
	"io"
)

// RequestReboot asks systemd for a clean reboot without waiting, then replies.
// Firecracker has no power-off and exits on a guest reboot.
func RequestReboot(ctx context.Context, connection io.Writer) error {
	return WriteReply(connection, runCommand(ctx, "systemctl", "reboot", "--no-block"))
}
