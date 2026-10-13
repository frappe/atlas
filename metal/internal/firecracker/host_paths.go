package firecracker

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// Config contains Firecracker host and binary paths.
type Config struct {
	MachinesDir    string
	SocketsDir     string
	JailerBin      string
	FirecrackerBin string
}

// DefaultConfig returns the standard Firecracker host paths.
func DefaultConfig() Config {
	return Config{
		MachinesDir:    "/var/lib/metal/machines",
		SocketsDir:     "/run/metal",
		JailerBin:      "/usr/bin/jailer",
		FirecrackerBin: "/usr/bin/firecracker",
	}
}

// FirecrackerVersion returns the version that the Firecracker binary prints,
// for example "v1.16.1".
func (configuration Config) FirecrackerVersion(ctx context.Context) (string, error) {
	output, err := platform.Output(ctx, configuration.FirecrackerBin, "--version")
	if err != nil {
		return "", err
	}
	// The first line is "Firecracker v1.16.1".
	fields := strings.Fields(output)
	if len(fields) < 2 {
		return "", fmt.Errorf("Firecracker printed no version: %q", output)
	}
	return fields[1], nil
}

// vmDir holds everything one VM owns on the host, so removing it removes the
// jail, the chroot, and the jailer environment together.
func (configuration Config) vmDir(id string) string {
	return filepath.Join(configuration.MachinesDir, id)
}
