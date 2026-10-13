package firecracker

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// Config contains Firecracker host paths and guest configuration.
type Config struct {
	MachinesDir    string
	SocketsDir     string
	JailerBin      string
	FirecrackerBin string
	CPUTemplate    json.RawMessage
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

// ConsolesDir holds the console PTY links of VMs and SSH sessions.
func (configuration Config) ConsolesDir() string {
	return filepath.Join(configuration.SocketsDir, "consoles")
}

// consolePath is the PTY link that a console unit uses as its terminal.
func (configuration Config) consolePath(id string) string {
	return filepath.Join(configuration.ConsolesDir(), id)
}

// sshSessionsDir holds one directory with the key and record of each SSH session.
func (configuration Config) sshSessionsDir() string {
	return filepath.Join(configuration.SocketsDir, "ssh-sessions")
}

func (configuration Config) sshSessionDir(sessionID string) string {
	return filepath.Join(configuration.sshSessionsDir(), sessionID)
}

// vmDir holds everything one VM owns on the host, so removing it removes the
// jail, the chroot, and the jailer environment together.
func (configuration Config) vmDir(id string) string {
	return filepath.Join(configuration.MachinesDir, id)
}
