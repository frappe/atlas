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
	cfg                firecracker.Config
	pool, imagesDir    string
	listen             string
	baseDir            string
	wireGuardName      string
	mesh               meshOptions
	trafficMonitor     trafficMonitorOptions
	migration          migrationOptions
	tls                tlsOptions
	coordinationListen string
}

type tlsOptions struct {
	caFile          string
	certificateFile string
	privateKeyFile  string
	atlasCommonName string
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
}

// trafficMonitorOptions configures VM traffic monitoring.
type trafficMonitorOptions struct {
	enabled bool
}

const defaultConfigPath = "/var/lib/metal/metald.toml"

const defaultBaseDir = "/var/lib/metal"

func defaultOptions() options {
	resolvedOptions := options{
		cfg:                firecracker.DefaultConfig(),
		pool:               "metal",
		baseDir:            defaultBaseDir,
		wireGuardName:      "wg0",
		mesh:               meshOptions{enabled: true, binaryPath: "/usr/local/bin/atlas-wg-mesh"},
		trafficMonitor:     trafficMonitorOptions{enabled: true},
		migration:          migrationOptions{finalDeltaMiB: 512, transferPort: 9002},
		coordinationListen: "127.0.0.1:9001",
		// TCP host:port by default; "unix:/path" for a unix socket instead.
		listen: "127.0.0.1:8080",
	}
	resolvedOptions.deriveDirs()
	return resolvedOptions
}

// deriveDirs places all metald directories under baseDir, so one base_dir moves the complete metald state tree.
func (resolvedOptions *options) deriveDirs() {
	resolvedOptions.cfg.MachinesDir = filepath.Join(resolvedOptions.baseDir, "machines")
	resolvedOptions.imagesDir = filepath.Join(resolvedOptions.baseDir, "images")
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
	TLS         tlsFile         `toml:"tls"`
}

type tlsFile struct {
	CAFile          string `toml:"ca_file"`
	CertificateFile string `toml:"certificate_file"`
	PrivateKeyFile  string `toml:"private_key_file"`
	AtlasCommonName string `toml:"atlas_common_name"`
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
	BaseDir            string `toml:"base_dir"`
	Listen             string `toml:"listen"`
	CoordinationListen string `toml:"coordination_listen"`
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
	overlay(&resolvedOptions.coordinationListen, fc.Metald.CoordinationListen)
	overlay(&resolvedOptions.cfg.FirecrackerBin, fc.Firecracker.BinaryPath)
	overlay(&resolvedOptions.cfg.SocketsDir, fc.Firecracker.SocketsDir)
	overlay(&resolvedOptions.cfg.JailerBin, fc.Jailer.BinaryPath)
	overlay(&resolvedOptions.pool, fc.ZFS.Pool)
	overlay(&resolvedOptions.wireGuardName, fc.WireGuard.Interface)
	overlayBool(&resolvedOptions.mesh.enabled, fc.WGMesh.Enabled)
	overlay(&resolvedOptions.mesh.binaryPath, fc.WGMesh.BinaryPath)
	overlay(&resolvedOptions.mesh.uplinkName, fc.WGMesh.Uplink)
	overlayBool(&resolvedOptions.trafficMonitor.enabled, fc.Traffic.Enabled)
	overlayInt(&resolvedOptions.migration.finalDeltaMiB, fc.Migration.FinalDeltaMiB)
	overlayInt(&resolvedOptions.migration.transferPort, fc.Migration.TransferPort)
	overlay(&resolvedOptions.tls.caFile, fc.TLS.CAFile)
	overlay(&resolvedOptions.tls.certificateFile, fc.TLS.CertificateFile)
	overlay(&resolvedOptions.tls.privateKeyFile, fc.TLS.PrivateKeyFile)
	overlay(&resolvedOptions.tls.atlasCommonName, fc.TLS.AtlasCommonName)
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
