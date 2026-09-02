// Command metald is the metal daemon: an HTTP server (over a unix socket) that
// drives firecracker microVMs.
//
//	metald serve   run the server (default)
//	metald up      idempotently bootstrap a /tmp dev host, then serve
package main

import (
	"context"
	_ "embed"
	"flag"
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
	// The subcommand is optional and comes first; anything else is a flag.
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	configPath, err := parseFlags(cmd, args)
	if err != nil {
		os.Exit(2)
	}
	switch cmd {
	case "serve":
		err = serve(configPath)
	case "up":
		err = up(configPath)
	default:
		fmt.Fprintf(os.Stderr, "usage: metald [serve|up] [--config path]\n")
		os.Exit(2)
	}
	if err != nil {
		log.Fatal(err)
	}
}

// parseFlags reads the shared --config flag for a subcommand. An empty path
// means no explicit config file was given.
func parseFlags(cmd string, args []string) (string, error) {
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	path := fs.String("config", "", "path to config.toml (optional; defaults to ./config.toml)")
	if err := fs.Parse(args); err != nil {
		return "", err
	}
	return *path, nil
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

func serve(configPath string) error {
	o, err := load(configPath)
	if err != nil {
		return err
	}
	units, err := systemd.Connect(context.Background())
	if err != nil {
		return fmt.Errorf("connect systemd: %w", err)
	}
	defer units.Close()

	driver := firecracker.New(o.cfg, units, storage.NewZFS(o.pool, o.kernelDir), network.NewLinux())
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
func up(configPath string) error {
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
	return serve(configPath)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func setDefault(k, v string) {
	if os.Getenv(k) == "" {
		_ = os.Setenv(k, v)
	}
}
