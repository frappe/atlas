package main

import (
	"errors"
	"fmt"
	"io/fs"
	"log"
	"path/filepath"

	"github.com/BurntSushi/toml"

	"github.com/frappe/atlas/metal/internal/firecracker"
)

// opts is the resolved metald configuration: the firecracker driver paths plus
// the storage pool, kernel dir, and API listen address.
type opts struct {
	cfg                        firecracker.Config
	pool, kernelDir, imagesDir string
	listen                     string
	baseDir                    string
}

const defaultConfigPath = "/var/lib/metal/metald.toml"

const defaultBaseDir = "/var/lib/metal"

// defaultOpts returns the built-in defaults.
func defaultOpts() opts {
	o := opts{
		cfg:     firecracker.DefaultConfig(),
		pool:    "metal",
		baseDir: defaultBaseDir,
		// TCP host:port by default; "unix:/path" for a unix socket instead.
		listen: "127.0.0.1:8080",
	}
	o.deriveDirs()
	return o
}

// deriveDirs places the directories metald owns under baseDir. They are a
// convention, not separate keys, so one base_dir moves all of them.
func (o *opts) deriveDirs() {
	o.cfg.MachinesDir = filepath.Join(o.baseDir, "machines")
	o.kernelDir = filepath.Join(o.baseDir, "kernels")
	// The warm-image memory store must share a filesystem with the jails, so a
	// mem file stages into a VM as a hard link.
	o.imagesDir = filepath.Join(o.baseDir, "images")
}

// fileConfig mirrors the optional metald configuration file. Sections group the
// keys by the package that owns them. An unset key keeps the default value.
type fileConfig struct {
	Metald      metaldFile      `toml:"metald"`
	Firecracker firecrackerFile `toml:"firecracker"`
	Jailer      jailerFile      `toml:"jailer"`
	ZFS         zfsFile         `toml:"zfs"`
}

type metaldFile struct {
	BaseDir string `toml:"base_dir"`
	Listen  string `toml:"listen"`
}

type firecrackerFile struct {
	BinaryPath string `toml:"binary_path"`
	SocketsDir string `toml:"sockets_dir"`
}

type jailerFile struct {
	BinaryPath string `toml:"binary_path"`
}

type zfsFile struct {
	Pool string `toml:"pool"`
}

// load resolves built-in defaults and the optional configuration file.
// An empty path uses the default configuration file.
func load(path string) (opts, error) {
	o := defaultOpts()
	if err := applyFile(&o, path); err != nil {
		return opts{}, err
	}
	o.deriveDirs()
	return o, nil
}

// applyFile overlays the configuration file onto o. A missing default file is
// not an error; a missing explicit path is.
func applyFile(o *opts, path string) error {
	explicit := path != ""
	if path == "" {
		path = defaultConfigPath
	}
	var fc fileConfig
	if _, err := toml.DecodeFile(path, &fc); err != nil {
		if errors.Is(err, fs.ErrNotExist) && !explicit {
			return nil
		}
		return fmt.Errorf("config %s: %w", path, err)
	}
	overlay(&o.baseDir, fc.Metald.BaseDir)
	overlay(&o.listen, fc.Metald.Listen)
	overlay(&o.cfg.FirecrackerBin, fc.Firecracker.BinaryPath)
	overlay(&o.cfg.SocketsDir, fc.Firecracker.SocketsDir)
	overlay(&o.cfg.JailerBin, fc.Jailer.BinaryPath)
	overlay(&o.pool, fc.ZFS.Pool)
	log.Printf("loaded config from %s", path)
	return nil
}

func overlay(dst *string, v string) {
	if v != "" {
		*dst = v
	}
}
