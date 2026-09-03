package firecracker

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"github.com/frappe/atlas/metal/internal/idalloc"
	"github.com/frappe/atlas/metal/internal/vm"
)

// Config contains Firecracker host paths and user IDs.
type Config struct {
	MachinesDir    string
	SocketsDir     string
	JailerBin      string
	FirecrackerBin string
	IDs            idalloc.Range
}

// DefaultConfig returns the standard Firecracker host paths.
func DefaultConfig() Config {
	return Config{
		MachinesDir:    "/var/lib/metal/machines",
		SocketsDir:     "/run/metal",
		JailerBin:      "/usr/bin/jailer",
		FirecrackerBin: "/usr/bin/firecracker",
		IDs:            idalloc.DefaultRange,
	}
}

// vmConfig stores one virtual machine reservation.
type vmConfig struct {
	ID           string        `json:"id"`
	UID          uint32        `json:"uid"`
	GID          uint32        `json:"gid"`
	IP           string        `json:"ip"`
	MAC          string        `json:"mac"`
	Sock         string        `json:"sock"`
	DesiredState vm.State      `json:"desired_state"`
	Cleanup      cleanupStatus `json:"cleanup,omitempty"`
	Spec         vm.Spec       `json:"spec"`
}

type cleanupStatus struct {
	Systemd bool `json:"systemd,omitempty"`
	Network bool `json:"network,omitempty"`
	Storage bool `json:"storage,omitempty"`
}

func (c Config) vmDir(id string) string      { return filepath.Join(c.MachinesDir, id) }
func (c Config) configPath(id string) string { return filepath.Join(c.vmDir(id), "config.json") }
func (c Config) statusPath(id string) string { return filepath.Join(c.vmDir(id), "status.json") }

// vmStatus stores observed state and reconciliation errors.
type vmStatus struct {
	State     vm.State  `json:"state"`
	Error     string    `json:"error,omitempty"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (c Config) writeStatus(id string, state vm.State, reconcileErr error) error {
	status := vmStatus{State: state, UpdatedAt: time.Now().UTC()}
	if reconcileErr != nil {
		status.Error = reconcileErr.Error()
	}
	b, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	return atomicWriteFile(c.statusPath(id), b, 0o640)
}

func (c Config) readStatus(id string) (vmStatus, bool) {
	b, err := os.ReadFile(c.statusPath(id))
	if err != nil {
		return vmStatus{}, false
	}
	var status vmStatus
	if err := json.Unmarshal(b, &status); err != nil {
		return vmStatus{}, false
	}
	return status, true
}

func (c Config) writeVMConfig(configuration vmConfig) error {
	data, err := json.MarshalIndent(configuration, "", "  ")
	if err != nil {
		return err
	}
	return atomicWriteFile(c.configPath(configuration.ID), data, 0o640)
}

func atomicWriteFile(path string, data []byte, mode fs.FileMode) error {
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return err
	}

	temporaryFile, err := os.CreateTemp(directory, "."+filepath.Base(path)+"-*")
	if err != nil {
		return err
	}
	temporaryPath := temporaryFile.Name()
	defer func() {
		_ = temporaryFile.Close()
		_ = os.Remove(temporaryPath)
	}()

	if err := temporaryFile.Chmod(mode); err != nil {
		return err
	}
	if _, err := temporaryFile.Write(data); err != nil {
		return err
	}
	if err := temporaryFile.Sync(); err != nil {
		return err
	}
	if err := temporaryFile.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	return syncDirectory(directory)
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func (c Config) readVMConfig(id string) (vmConfig, error) {
	b, err := os.ReadFile(c.configPath(id))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return vmConfig{}, vm.ErrNotFound
		}
		return vmConfig{}, err
	}
	var vc vmConfig
	return vc, json.Unmarshal(b, &vc)
}

func (c Config) listVMIDs() ([]string, error) {
	entries, err := os.ReadDir(c.MachinesDir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			ids = append(ids, e.Name())
		}
	}
	return ids, nil
}

// usedIDs returns assigned user IDs.
func (c Config) usedIDs() (map[uint32]bool, error) {
	ids, err := c.listVMIDs()
	if err != nil {
		return nil, err
	}
	used := make(map[uint32]bool, len(ids))
	for _, id := range ids {
		configuration, err := c.readVMConfig(id)
		if err != nil {
			return nil, err
		}
		used[configuration.UID] = true
	}
	return used, nil
}
