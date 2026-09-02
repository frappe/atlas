// Package hostcmd runs host commands and folds their output into errors. It is
// shared by the drivers that shell out to host tools, such as storage (zfs) and
// network (ip).
package hostcmd

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// Run executes a command and folds its combined output into the error, so a
// failure carries the tool's own message.
func Run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// Output runs a command and returns its stdout. The error carries stderr, so the
// caller can classify the failure.
func Output(ctx context.Context, name string, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
