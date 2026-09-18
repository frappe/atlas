package main

import (
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"time"

	"github.com/BurntSushi/toml"

	"github.com/frappe/atlas/metal/internal/firecracker"
)

type options struct {
	cfg             firecracker.Config
	pool, imagesDir string
	listen          string
	authTokenHash   string
	baseDir         string
	wireGuardName   string
	mesh            meshOptions
	trafficMonitor  trafficMonitorOptions
	migration       migrationOptions
}

// migrationOptions holds VM migration settings.
type migrationOptions struct {
	finalDeltaMiB int
	// transferPort is the TCP port the source listens on for disk streams.
	transferPort int
}

// meshOptions configures Atlas WG Mesh.
type meshOptions struct {
	enabled    bool
	binaryPath string
	uplinkName string
	// unicastPeersFile is empty when the config file sets no path.
	unicastPeersFile string
}

// trafficMonitorOptions configures VM traffic monitoring.
type trafficMonitorOptions struct {
	enabled bool
}

const defaultConfigPath = "/var/lib/metal/metald.toml"

const defaultBaseDir = "/var/lib/metal"

func defaultOptions() options {
	resolvedOptions := options{
		cfg:            firecracker.DefaultConfig(),
		pool:           "metal",
		baseDir:        defaultBaseDir,
		wireGuardName:  "wg0",
		mesh:           meshOptions{enabled: true, binaryPath: "/usr/local/bin/atlas-wg-mesh"},
		trafficMonitor: trafficMonitorOptions{enabled: true},
		migration:      migrationOptions{finalDeltaMiB: 512, transferPort: 9001},
		// TCP host:port by default; "unix:/path" for a unix socket instead.
		listen: "127.0.0.1:8080",
	}
	resolvedOptions.deriveDirs()
	return resolvedOptions
}

// deriveDirs places all metald directories under baseDir, so one base_dir moves
// the complete metald state tree.
func (resolvedOptions *options) deriveDirs() {
	resolvedOptions.cfg.MachinesDir = filepath.Join(resolvedOptions.baseDir, "machines")
	resolvedOptions.imagesDir = filepath.Join(resolvedOptions.baseDir, "images")
}

// unicastPeersFilePath places the unicast peer file under baseDir when the
// config file sets no path.
func (resolvedOptions options) unicastPeersFilePath() string {
	if resolvedOptions.mesh.unicastPeersFile != "" {
		return resolvedOptions.mesh.unicastPeersFile
	}
	return filepath.Join(resolvedOptions.baseDir, "unicast-peers")
}

type fileConfig struct {
	Metald      metaldFile      `toml:"metald"`
	Firecracker firecrackerFile `toml:"firecracker"`
	Jailer      jailerFile      `toml:"jailer"`
	ZFS         zfsFile         `toml:"zfs"`
	WireGuard   wireGuardFile   `toml:"wireguard"`
	WGMesh      wgMeshFile      `toml:"wg_mesh"`
	Traffic     trafficFile     `toml:"traffic_monitor"`
	Migration   migrationFile   `toml:"migration"`
}

// tomlDuration decodes a TOML string with time.ParseDuration.
type tomlDuration struct {
	time.Duration
}

// UnmarshalText parses a duration string such as "30m".
func (duration *tomlDuration) UnmarshalText(text []byte) error {
	parsed, err := time.ParseDuration(string(text))
	if err != nil {
		return err
	}
	duration.Duration = parsed
	return nil
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
	Enabled    *bool  `toml:"enabled"`
	BinaryPath string `toml:"binary_path"`
	Uplink     string `toml:"uplink"`
	PeersFile  string `toml:"peers_file"`
}

type trafficFile struct {
	Enabled *bool `toml:"enabled"`
}

type migrationFile struct {
	FinalDeltaMiB *int `toml:"final_delta_mib"`
	TransferPort  *int `toml:"transfer_port"`
}

func load(path string) (options, error) {
	resolvedOptions := defaultOptions()
	if err := applyFile(&resolvedOptions, path); err != nil {
		return options{}, err
	}
	resolvedOptions.deriveDirs()
	return resolvedOptions, nil
}

// applyFile overlays a configuration file onto resolvedOptions. A missing default file is
// allowed; a missing explicit file is an error.
func applyFile(resolvedOptions *options, path string) error {
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
	overlay(&resolvedOptions.baseDir, fc.Metald.BaseDir)
	overlay(&resolvedOptions.listen, fc.Metald.Listen)
	overlay(&resolvedOptions.authTokenHash, fc.Metald.AuthTokenHash)
	overlay(&resolvedOptions.cfg.FirecrackerBin, fc.Firecracker.BinaryPath)
	overlay(&resolvedOptions.cfg.SocketsDir, fc.Firecracker.SocketsDir)
	overlay(&resolvedOptions.cfg.JailerBin, fc.Jailer.BinaryPath)
	overlay(&resolvedOptions.pool, fc.ZFS.Pool)
	overlay(&resolvedOptions.wireGuardName, fc.WireGuard.Interface)
	overlayBool(&resolvedOptions.mesh.enabled, fc.WGMesh.Enabled)
	overlay(&resolvedOptions.mesh.binaryPath, fc.WGMesh.BinaryPath)
	overlay(&resolvedOptions.mesh.uplinkName, fc.WGMesh.Uplink)
	overlay(&resolvedOptions.mesh.unicastPeersFile, fc.WGMesh.PeersFile)
	overlayBool(&resolvedOptions.trafficMonitor.enabled, fc.Traffic.Enabled)
	overlayInt(&resolvedOptions.migration.finalDeltaMiB, fc.Migration.FinalDeltaMiB)
	overlayInt(&resolvedOptions.migration.transferPort, fc.Migration.TransferPort)
	return nil
}

func overlay(dst *string, v string) {
	if v != "" {
		*dst = v
	}
}

func overlayBool(destination *bool, value *bool) {
	if value != nil {
		*destination = *value
	}
}

func overlayInt(destination *int, value *int) {
	if value != nil {
		*destination = *value
	}
}
