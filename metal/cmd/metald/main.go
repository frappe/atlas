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
	"time"

	"github.com/frappe/atlas/metal/internal/api"
	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/firecracker"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/reconciler"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/systemd"
)

const (
	reconcileInterval      = 5 * time.Second
	imageReconcileInterval = time.Hour
)

//	@title			Metal HTTP application programming interface
//	@version		1.0
//	@description	metald manages Firecracker micro virtual machines on one host.
//	@BasePath		/
//
//	@securityDefinitions.apikey	BearerAuth
//	@in							header
//	@name						Authorization
//	@description				Type "Bearer" then a space and the API token.

// version is the build version set with -ldflags "-X main.version=...".
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

func makeDirs(o opts) error {
	dirs := []struct {
		path string
		mode os.FileMode
	}{
		{o.cfg.MachinesDir, 0o750},
		{o.cfg.SocketsDir, 0o700},
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
	if o.authTokenHash == "" {
		return fmt.Errorf("metald.auth_token_hash is required")
	}
	if err := makeDirs(o); err != nil {
		return err
	}
	units, err := systemd.Connect(context.Background())
	if err != nil {
		return fmt.Errorf("connect systemd: %w", err)
	}
	defer units.Close()

	stores := storage.NewStores(o.pool, o.imagesDir)
	wireGuardManager, err := network.NewWireGuardManager(network.WireGuardConfig{
		InterfaceName: "wg0",
		StatePath:     filepath.Join(o.baseDir, "wireguard-peers.json"),
	})
	if err != nil {
		return fmt.Errorf("configure WireGuard manager: %w", err)
	}
	consoleBroker := console.NewBroker(filepath.Join(o.cfg.SocketsDir, "consoles"))
	defer consoleBroker.Shutdown()

	virtualMachineDriver := firecracker.New(
		o.cfg,
		units,
		stores.VirtualMachines,
		stores.Images,
		stores.Snapshots,
		network.NewLinuxAllocator(),
		consoleBroker,
	)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	virtualMachineReconciler := reconciler.New(
		virtualMachineDriver,
		reconcileInterval,
		reconciler.Config{},
	)
	imageReconciler := reconciler.NewImageReconciler(
		stores.Images,
		stores.Snapshots,
		virtualMachineDriver,
		imageReconcileInterval,
		reconciler.ImageConfig{},
	)
	go virtualMachineReconciler.Run(ctx)
	go imageReconciler.Run(ctx)

	wakeReconcilers := func() {
		virtualMachineReconciler.Wake()
		imageReconciler.Wake()
	}
	server, err := api.New(api.Config{AuthTokenHash: o.authTokenHash}, api.Dependencies{
		VirtualMachineDriver: virtualMachineDriver,
		SnapshotCreator:      virtualMachineDriver,
		SnapshotStore:        stores.Snapshots,
		ImagePolicyStore:     stores.Images,
		WakeReconciler:       wakeReconcilers,
		WireGuardManager:     wireGuardManager,
		Storage:              stores.Pool,
		ConsoleBroker:        consoleBroker,
		SSHConnector:         virtualMachineDriver,
	})
	if err != nil {
		return fmt.Errorf("configure API: %w", err)
	}

	listener, err := listen(o.listen)
	if err != nil {
		return fmt.Errorf("listen %s: %w", o.listen, err)
	}
	log.Printf("metald %s listening on %s", version, o.listen)
	server.Listener = listener
	if err := server.Start(""); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}
