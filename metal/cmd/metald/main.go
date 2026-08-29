// Command metald is the metal daemon: an HTTP server (over a unix socket) that
// drives firecracker microVMs.
//
//	metald serve   run the server (default)
//	metald up      idempotently bootstrap a /tmp dev host, then serve
package main

import (
	"context"
	_ "embed"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/frappe/atlas/metal/internal/api"
	"github.com/frappe/atlas/metal/internal/firecracker"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
)

//go:embed up.sh
var upScript string

func main() {
	cmd := "serve"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve()
	case "up":
		err = up()
	default:
		fmt.Fprintf(os.Stderr, "usage: metald [serve|up]\n")
		os.Exit(2)
	}
	if err != nil {
		log.Fatal(err)
	}
}

type opts struct {
	cfg           firecracker.Config
	vg, kernelDir string
	listen        string
}

func optsFromEnv() opts {
	cfg := firecracker.DefaultConfig()
	setIf(&cfg.ChrootBase, "METALD_CHROOT_BASE")
	setIf(&cfg.VarDir, "METALD_VAR_DIR")
	setIf(&cfg.JailerBin, "METALD_JAILER")
	setIf(&cfg.FirecrackerBin, "METALD_FIRECRACKER")
	return opts{
		cfg:       cfg,
		vg:        envOr("METALD_VG", "metalvg"),
		kernelDir: envOr("METALD_KERNEL_DIR", "/var/lib/metal/kernels"),
		// TCP host:port by default; "unix:/path" for a unix socket instead.
		listen: envOr("METALD_LISTEN", "127.0.0.1:8080"),
	}
}

// listen opens the API listener. addr is a TCP host:port, or "unix:/path".
func listen(addr string) (net.Listener, error) {
	if path, ok := strings.CutPrefix(addr, "unix:"); ok {
		_ = os.Remove(path)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, err
		}
		ln, err := net.Listen("unix", path)
		if err == nil {
			_ = os.Chmod(path, 0o660)
		}
		return ln, err
	}
	return net.Listen("tcp", addr)
}

func serve() error {
	o := optsFromEnv()
	units, err := systemd.Connect(context.Background())
	if err != nil {
		return fmt.Errorf("connect systemd: %w", err)
	}
	defer units.Close()

	driver := firecracker.New(o.cfg, units, storage.NewLVM(o.vg, o.kernelDir), network.NewLinux())
	e := api.New(driver)

	ln, err := listen(o.listen)
	if err != nil {
		return fmt.Errorf("listen %s: %w", o.listen, err)
	}
	log.Printf("metald listening on %s", o.listen)
	e.Listener = ln
	if err := e.Start(""); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}

// up bootstraps a throwaway dev host under a /tmp workdir, then serves. Env
// defaults point every path into the workdir so it never touches system dirs.
func up() error {
	workdir := envOr("METALD_WORKDIR", "/tmp/metald")
	setDefault("METALD_WORKDIR", workdir)
	setDefault("METALD_CHROOT_BASE", filepath.Join(workdir, "chroot"))
	setDefault("METALD_VAR_DIR", filepath.Join(workdir, "vms"))
	setDefault("METALD_KERNEL_DIR", filepath.Join(workdir, "kernels"))
	setDefault("METALD_LISTEN", "127.0.0.1:8080")
	setDefault("METALD_JAILER", filepath.Join(workdir, "bin", "jailer"))
	setDefault("METALD_FIRECRACKER", filepath.Join(workdir, "bin", "firecracker"))

	c := exec.Command("bash", "-s")
	c.Stdin = strings.NewReader(upScript)
	c.Stdout, c.Stderr = os.Stdout, os.Stderr
	c.Env = os.Environ()
	if err := c.Run(); err != nil {
		return fmt.Errorf("bootstrap: %w", err)
	}
	return serve()
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func setIf(dst *string, k string) {
	if v := os.Getenv(k); v != "" {
		*dst = v
	}
}

func setDefault(k, v string) {
	if os.Getenv(k) == "" {
		_ = os.Setenv(k, v)
	}
}
