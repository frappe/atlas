package firecracker

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/frappe/metal/internal/idalloc"
	"github.com/frappe/metal/internal/vm"
)

// Config holds the driver's host paths and id range.
type Config struct {
	ChrootBase     string // jailer chroot base dir, e.g. /srv/jailer
	VarDir         string // per-VM state dir, e.g. /var/lib/metal/vms
	JailerBin      string
	FirecrackerBin string
	IDs            idalloc.Range
}

func DefaultConfig() Config {
	return Config{
		ChrootBase:     "/srv/jailer",
		VarDir:         "/var/lib/metal/vms",
		JailerBin:      "/usr/bin/jailer",
		FirecrackerBin: "/usr/bin/firecracker",
		IDs:            idalloc.DefaultRange,
	}
}

// vmConfig is the per-VM state persisted under VarDir/<id>. It is co-located
// with the VM (not a central store), so metald stays stateless: List/Load
// reconstruct handles from these files plus systemd.
type vmConfig struct {
	ID   string  `json:"id"`
	UID  uint32  `json:"uid"`
	GID  uint32  `json:"gid"`
	IP   string  `json:"ip"`
	MAC  string  `json:"mac"`
	Sock string  `json:"sock"`
	Spec vm.Spec `json:"spec"`
}

func (c Config) vmDir(id string) string      { return filepath.Join(c.VarDir, id) }
func (c Config) configPath(id string) string { return filepath.Join(c.vmDir(id), "config.json") }

func (c Config) writeVMConfig(vc vmConfig) error {
	if err := os.MkdirAll(c.vmDir(vc.ID), 0o750); err != nil {
		return err
	}
	b, err := json.MarshalIndent(vc, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(c.configPath(vc.ID), b, 0o640)
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

// listVMIDs returns the ids of every VM with persisted state.
func (c Config) listVMIDs() ([]string, error) {
	entries, err := os.ReadDir(c.VarDir)
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

// usedIDs reconstructs the set of allocated uids by reading every VM's config,
// so allocation needs no separate state.
func (c Config) usedIDs() (map[uint32]bool, error) {
	ids, err := c.listVMIDs()
	if err != nil {
		return nil, err
	}
	used := make(map[uint32]bool, len(ids))
	for _, id := range ids {
		vc, err := c.readVMConfig(id)
		if err != nil {
			continue // a half-written dir shouldn't block allocation
		}
		used[vc.UID] = true
	}
	return used, nil
}
