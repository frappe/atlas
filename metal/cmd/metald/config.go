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

type opts struct {
	cfg             firecracker.Config
	pool, imagesDir string
	listen          string
	authTokenHash   string
	baseDir         string
	wireGuardName   string
	mesh            meshOpts
}

// meshOpts configures the Atlas WG Mesh integration. An empty uplink name uses
// the interface of the host default route.
type meshOpts struct {
	enabled    bool
	binaryPath string
	uplinkName string
}

const defaultConfigPath = "/var/lib/metal/metald.toml"

const defaultBaseDir = "/var/lib/metal"

func defaultOpts() opts {
	o := opts{
		cfg:           firecracker.DefaultConfig(),
		pool:          "metal",
		baseDir:       defaultBaseDir,
		wireGuardName: "wg0",
		mesh:          meshOpts{binaryPath: "/usr/local/bin/atlas-wg-mesh"},
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
	o.imagesDir = filepath.Join(o.baseDir, "images")
}

type fileConfig struct {
	Metald      metaldFile      `toml:"metald"`
	Firecracker firecrackerFile `toml:"firecracker"`
	Jailer      jailerFile      `toml:"jailer"`
	ZFS         zfsFile         `toml:"zfs"`
	WireGuard   wireGuardFile   `toml:"wireguard"`
	WGMesh      wgMeshFile      `toml:"wg_mesh"`
}

type metaldFile struct {
	BaseDir       string `toml:"base_dir"`
	Listen        string `toml:"listen"`
	AuthTokenHash string `toml:"auth_token_hash"`
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

type wireGuardFile struct {
	Interface string `toml:"interface"`
}

type wgMeshFile struct {
	Enabled    bool   `toml:"enabled"`
	BinaryPath string `toml:"binary_path"`
	Uplink     string `toml:"uplink"`
}

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
	overlay(&o.authTokenHash, fc.Metald.AuthTokenHash)
	overlay(&o.cfg.FirecrackerBin, fc.Firecracker.BinaryPath)
	overlay(&o.cfg.SocketsDir, fc.Firecracker.SocketsDir)
	overlay(&o.cfg.JailerBin, fc.Jailer.BinaryPath)
	overlay(&o.pool, fc.ZFS.Pool)
	overlay(&o.wireGuardName, fc.WireGuard.Interface)
	overlay(&o.mesh.binaryPath, fc.WGMesh.BinaryPath)
	overlay(&o.mesh.uplinkName, fc.WGMesh.Uplink)
	o.mesh.enabled = fc.WGMesh.Enabled
	log.Printf("loaded config from %s", path)
	return nil
}

func overlay(dst *string, v string) {
	if v != "" {
		*dst = v
	}
}
