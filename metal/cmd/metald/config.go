package main

import (
	"errors"
	"fmt"
	"io/fs"
	"log"
	"os"

	"github.com/BurntSushi/toml"

	"github.com/frappe/atlas/metal/internal/firecracker"
)

// opts is the resolved metald configuration: the firecracker driver paths plus
// the storage pool, kernel dir, and API listen address.
type opts struct {
	cfg                        firecracker.Config
	pool, kernelDir, imagesDir string
	listen                     string
}

// defaultOpts returns the built-in defaults. This is the lowest config layer.
func defaultOpts() opts {
	return opts{
		cfg:       firecracker.DefaultConfig(),
		pool:      "metal",
		kernelDir: "/var/lib/metal/kernels",
		imagesDir: "/var/lib/metal/images",
		// TCP host:port by default; "unix:/path" for a unix socket instead.
		listen: "127.0.0.1:8080",
	}
}

// fileConfig mirrors the optional config.toml. Sections group the keys by the
// package that owns them. An unset key keeps the value from the layer below.
type fileConfig struct {
	Listen      string          `toml:"listen"`
	Firecracker firecrackerFile `toml:"firecracker"`
	Storage     storageFile     `toml:"storage"`
}

type firecrackerFile struct {
	ChrootBase     string `toml:"chroot_base"`
	VarDir         string `toml:"var_dir"`
	JailerBin      string `toml:"jailer_bin"`
	FirecrackerBin string `toml:"firecracker_bin"`
}

type storageFile struct {
	Pool      string `toml:"pool"`
	KernelDir string `toml:"kernel_dir"`
	ImagesDir string `toml:"images_dir"`
}

// load resolves the configuration in three layers, lowest to highest: built-in
// defaults, the optional config.toml, then the environment. If path is empty,
// "config.toml" in the working dir is used when it exists; a path given
// explicitly must exist.
func load(path string) (opts, error) {
	o := defaultOpts()
	if err := applyFile(&o, path); err != nil {
		return opts{}, err
	}
	applyEnv(&o)
	return o, nil
}

// applyFile overlays config.toml onto o. A missing default file is not an error;
// a missing explicit path is.
func applyFile(o *opts, path string) error {
	explicit := path != ""
	if path == "" {
		path = "config.toml"
	}
	var fc fileConfig
	if _, err := toml.DecodeFile(path, &fc); err != nil {
		if errors.Is(err, fs.ErrNotExist) && !explicit {
			return nil
		}
		return fmt.Errorf("config %s: %w", path, err)
	}
	overlay(&o.listen, fc.Listen)
	overlay(&o.cfg.ChrootBase, fc.Firecracker.ChrootBase)
	overlay(&o.cfg.VarDir, fc.Firecracker.VarDir)
	overlay(&o.cfg.JailerBin, fc.Firecracker.JailerBin)
	overlay(&o.cfg.FirecrackerBin, fc.Firecracker.FirecrackerBin)
	overlay(&o.pool, fc.Storage.Pool)
	overlay(&o.kernelDir, fc.Storage.KernelDir)
	overlay(&o.imagesDir, fc.Storage.ImagesDir)
	log.Printf("loaded config from %s", path)
	return nil
}

// overlay sets *dst to v when v is non-empty, so an unset value is a no-op.
func overlay(dst *string, v string) {
	if v != "" {
		*dst = v
	}
}

// applyEnv overlays the METALD_* environment variables onto o. This is the
// highest config layer, so a set env var wins over the file and the defaults.
// Use setIf for each field. The env vars are METALD_CHROOT_BASE,
// METALD_VAR_DIR, METALD_JAILER, METALD_FIRECRACKER, METALD_POOL,
// METALD_KERNEL_DIR, METALD_IMAGES_DIR, and METALD_LISTEN.
func applyEnv(o *opts) {
	setIf(&o.cfg.ChrootBase, "METALD_CHROOT_BASE")
	setIf(&o.cfg.VarDir, "METALD_VAR_DIR")
	setIf(&o.cfg.JailerBin, "METALD_JAILER")
	setIf(&o.cfg.FirecrackerBin, "METALD_FIRECRACKER")
	setIf(&o.pool, "METALD_POOL")
	setIf(&o.kernelDir, "METALD_KERNEL_DIR")
	setIf(&o.imagesDir, "METALD_IMAGES_DIR")
	setIf(&o.listen, "METALD_LISTEN")
}

// setIf sets *dst to the value of env var k when k is set and non-empty.
func setIf(dst *string, k string) {
	if v := os.Getenv(k); v != "" {
		*dst = v
	}
}
