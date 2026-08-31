package storage

// Helpers for shelling out to the zfs/zpool CLIs and classifying their errors.

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// run executes a command, folding its output into the error for context.
func run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// output runs a command and returns its stdout; the error carries stderr so
// notFoundAware can classify it.
func output(ctx context.Context, name string, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

// notFoundAware maps a ZFS "does not exist" failure to ErrNotFound.
func notFoundAware(err error) error {
	if err != nil && strings.Contains(err.Error(), "does not exist") {
		return ErrNotFound
	}
	return err
}
