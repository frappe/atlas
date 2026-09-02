// Command metald is the metal daemon: an HTTP server (over a unix socket) that
// drives firecracker microVMs.
//
//	metald serve [--config path]   run the server (default)
//
// Use scripts/dev.sh to prepare a throwaway dev host before serve.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/frappe/atlas/metal/internal/api"
	"github.com/frappe/atlas/metal/internal/firecracker"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
)

//	@title			Metal HTTP application programming interface
//	@version		1.0
//	@description	metald manages Firecracker micro virtual machines on one host.
//	@BasePath		/

// version is the build version
// set with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	configPath, err := parseFlags(cmd, args)
	if err != nil {
		os.Exit(2)
	}
	if cmd != "serve" {
		fmt.Fprintf(os.Stderr, "usage: metald [serve] [--config path]\n")
		os.Exit(2)
	}
	o, err := load(configPath)
	if err == nil {
		err = serve(o)
	}
	if err != nil {
		log.Fatal(err)
	}
}

func parseFlags(cmd string, args []string) (string, error) {
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	path := fs.String("config", "", "path to the configuration file (optional; defaults to "+defaultConfigPath+")")
	if err := fs.Parse(args); err != nil {
		return "", err
	}
	return *path, nil
}

// listen opens the application programming interface listener. addr is a TCP
// host:port, or "unix:/path".
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

// makeDirs creates the directories the config names. metald makes most of them
// when it starts a VM, but a fresh host has no kernel dir at all, and a missing
// one only shows up as a link failure on the first create.
func makeDirs(o opts) error {
	dirs := []struct {
		path string
		mode os.FileMode
	}{
		{o.cfg.MachinesDir, 0o750},
		{o.cfg.SocketsDir, 0o700},
		{o.kernelDir, 0o755},
		{o.imagesDir, 0o755},
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d.path, d.mode); err != nil {
			return fmt.Errorf("create %s: %w", d.path, err)
		}
	}
	return nil
}

func serve(o opts) error {
	if err := makeDirs(o); err != nil {
		return err
	}
	units, err := systemd.Connect(context.Background())
	if err != nil {
		return fmt.Errorf("connect systemd: %w", err)
	}
	defer units.Close()

	driver := firecracker.New(o.cfg, units, storage.NewZFS(o.pool, o.kernelDir, o.imagesDir), network.NewLinux())
	e := api.New(driver)

	ln, err := listen(o.listen)
	if err != nil {
		return fmt.Errorf("listen %s: %w", o.listen, err)
	}
	log.Printf("metald %s listening on %s", version, o.listen)
	e.Listener = ln
	if err := e.Start(""); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}
