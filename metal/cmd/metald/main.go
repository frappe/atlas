// Command metald is the metal daemon: an HTTP server (over a unix socket) that
// drives firecracker microVMs.
//
//	metald serve [--config path]   run the server (default)
//	metald version                 print the build version
//
// Use scripts/dev.sh to prepare a throwaway dev host before serve.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/api"
	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/firecracker"
	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/network"
	traffic "github.com/frappe/atlas/metal/internal/network/traffic"
	platform "github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/reconciler"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
	vmmigration "github.com/frappe/atlas/metal/internal/vm_migration"
)

const (
	reconcileInterval      = 5 * time.Second
	meshSetupTimeout       = 2 * time.Minute
	imageReconcileInterval = time.Hour
)

//	@title			Metal API
//	@version		1.0
//	@description	Metal manages Firecracker virtual machines and host resources.
//	@BasePath		/
//
//	@tag.name		Virtual machines
//	@tag.description	Manage desired and observed virtual machine state.
//	@tag.name		Snapshots
//	@tag.description	Stage and upload virtual machine image artifacts.
//	@tag.name		Host synchronization
//	@tag.description	Replace controller-owned host state and get capacity.
//	@tag.name		Health
//	@tag.description	Check the Metal HTTP server.
//
//	@securityDefinitions.apikey	BearerAuth
//	@in							header
//	@name						Authorization
//	@description				Use "Bearer", one space, and the API token.

// version is the build version set with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	if cmd == "version" {
		fmt.Println(version)
		return
	}
	configPath, err := parseFlags(cmd, args)
	if err != nil {
		os.Exit(2)
	}
	if cmd != "serve" {
		fmt.Fprintf(os.Stderr, "usage: metald [serve] [--config path] | metald version\n")
		os.Exit(2)
	}
	options, err := load(configPath)
	if err == nil {
		err = serve(options, logger)
	}
	if err != nil {
		logger.Error("metald stopped", "error", err)
		os.Exit(1)
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

// transferListenAddress derives the migration transfer address from the API
// listen address, keeping its host but using the transfer port. A unix socket
// API address falls back to every interface.
func transferListenAddress(apiAddress string, transferPort int) string {
	port := fmt.Sprintf("%d", transferPort)
	if strings.HasPrefix(apiAddress, "unix:") {
		return ":" + port
	}
	host, _, err := net.SplitHostPort(apiAddress)
	if err != nil {
		return ":" + port
	}
	return net.JoinHostPort(host, port)
}

func makeDirs(options options) error {
	dirs := []struct {
		path string
		mode os.FileMode
	}{
		{options.cfg.MachinesDir, 0o750},
		{options.cfg.SocketsDir, 0o700},
		{options.imagesDir, 0o755},
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d.path, d.mode); err != nil {
			return fmt.Errorf("create %s: %w", d.path, err)
		}
	}
	return nil
}

// connectMesh prepares Atlas WG Mesh and configures the host on every start.
func connectMesh(options options) (*network.Mesh, error) {
	mesh, err := network.NewMesh(network.MeshConfig{
		CommandPath:   options.mesh.binaryPath,
		UplinkName:    options.mesh.uplinkName,
		WireGuardName: options.wireGuardName,
	})
	if err != nil {
		return nil, fmt.Errorf("configure Atlas WG Mesh: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), meshSetupTimeout)
	defer cancel()
	if err := mesh.EnsureHost(ctx); err != nil {
		return nil, fmt.Errorf("configure Atlas WG Mesh host: %w", err)
	}
	return mesh, nil
}

// adoptConsoles restores consoles after a metald restart.
func adoptConsoles(
	ctx context.Context,
	logger *slog.Logger,
	serialBroker *console.SerialBroker,
	units *platform.DBus,
	descriptorStoreAvailable bool,
) error {
	runningVirtualMachineIDs, err := units.List(ctx)
	if err != nil {
		return fmt.Errorf("list virtual machine units: %w", err)
	}

	adoptedConsoleCount := serialBroker.Adopt(runningVirtualMachineIDs)
	logger.Info(
		"adopted serial consoles",
		"adopted_console_count", adoptedConsoleCount,
		"running_unit_count", len(runningVirtualMachineIDs),
		"descriptor_store_available", descriptorStoreAvailable,
	)

	// Without the store, metald holds the only PTY master. Its exit stops every VM.
	if !descriptorStoreAvailable {
		logger.Warn(
			"descriptor store unavailable; a metald exit stops every running VM",
			"running_unit_count", len(runningVirtualMachineIDs),
			"required_unit_settings", "NotifyAccess=main, FileDescriptorStoreMax",
		)

		return nil
	}

	// A VM without an adopted console has no stored master. Its next metald exit stops it.
	if adoptedConsoleCount < len(runningVirtualMachineIDs) {
		logger.Warn(
			"running VMs have no adopted console; a metald exit stops them",
			"adopted_console_count", adoptedConsoleCount,
			"running_unit_count", len(runningVirtualMachineIDs),
		)
	}

	return nil
}

func serve(options options, logger *slog.Logger) (serveError error) {
	if options.authTokenHash == "" {
		return fmt.Errorf("metald.auth_token_hash is required")
	}

	// Connect required host services.
	var err error
	var mesh *network.Mesh
	if options.mesh.enabled {
		mesh, err = connectMesh(options)
		if err != nil {
			return err
		}
	}
	if err := makeDirs(options); err != nil {
		return err
	}
	units, err := platform.Connect(context.Background())
	if err != nil {
		return fmt.Errorf("connect systemd: %w", err)
	}

	// Create resources that metald owns.
	daemonContext, cancelDaemon := context.WithCancel(context.Background())
	stores := storage.NewStores(daemonContext, options.pool, options.imagesDir, logger)
	descriptorStore := platform.NewFileDescriptorStore()
	serialBroker := console.NewSerialBroker(filepath.Join(options.cfg.SocketsDir, "consoles"), descriptorStore)
	daemon := newDaemon(daemonContext, cancelDaemon, logger, stores.Snapshots, serialBroker, units)
	defer func() {
		shutdownContext, cancelShutdown := context.WithTimeout(context.Background(), gracefulShutdownTimeout)
		defer cancelShutdown()
		serveError = errors.Join(serveError, daemon.Shutdown(shutdownContext))
	}()

	// Restore consoles for running virtual machines.
	if err := adoptConsoles(daemonContext, logger, serialBroker, units, descriptorStore.IsAvailable()); err != nil {
		return err
	}

	// Create virtual machine and API services.
	wireGuardManager, err := network.NewWireGuardManager(network.WireGuardConfig{
		InterfaceName: options.wireGuardName,
		StatePath:     filepath.Join(options.baseDir, "wireguard-peers.json"),
	})
	if err != nil {
		return fmt.Errorf("configure WireGuard manager: %w", err)
	}
	// The unicast transport needs the uplink that Atlas WG Mesh owns, so it is
	// absent when the mesh is disabled and a unicast sync then fails loudly.
	var unicastManager *network.UnicastManager
	if options.mesh.enabled {
		unicastManager, err = network.NewUnicastManager(network.UnicastConfig{
			PeersFilePath: options.unicastPeersFilePath(),
			UplinkName:    options.mesh.uplinkName,
		})
		if err != nil {
			return fmt.Errorf("configure unicast manager: %w", err)
		}
	}
	var trafficMonitor *traffic.Monitor
	if options.trafficMonitor.enabled {
		trafficMonitor, err = traffic.NewMonitor(traffic.Config{
			MinimumUserID: vm.DefaultUserIDRange.Min,
			MaximumUserID: vm.DefaultUserIDRange.Max,
			Logger:        logger,
		})
		if err != nil {
			return fmt.Errorf("configure traffic monitor: %w", err)
		}
		daemon.OwnTrafficMonitor(trafficMonitor)
	}
	networkManager := network.NewLinuxAllocator(mesh, trafficMonitor)
	virtualMachineRuntime := firecracker.NewRuntime(
		options.cfg,
		units,
		stores.VirtualMachines,
		stores.Images,
		serialBroker,
		logger,
	)
	virtualMachineManager, err := vm.NewManager(
		vm.ManagerConfig{
			MachinesDirectory: options.cfg.MachinesDir,
		},
		vm.ManagerDependencies{
			Runtime:   virtualMachineRuntime,
			Network:   networkManager,
			Storage:   stores.VirtualMachines,
			Snapshots: stores.Snapshots,
			Traffic:   trafficMonitor,
			Logger:    logger,
		},
	)
	if err != nil {
		return fmt.Errorf("configure VM manager: %w", err)
	}
	memorySnapshotBuilder := vm.NewWarmImageBuilder(
		virtualMachineManager,
		virtualMachineRuntime,
		stores.Images,
	)

	virtualMachineReconciler := reconciler.NewVirtualMachineReconciler(
		virtualMachineManager,
		reconcileInterval,
		reconciler.VirtualMachineConfig{Logger: logger},
	)
	imageReconciler := reconciler.NewImageReconciler(
		stores.Images,
		stores.Snapshots,
		memorySnapshotBuilder,
		imageReconcileInterval,
		reconciler.ImageConfig{Logger: logger},
	)
	var migrationReconciler *reconciler.MigrationReconciler
	var migrationManager *vmmigration.VMMigration
	notifyReconcilers := func() {
		virtualMachineReconciler.Wake()
		imageReconciler.Wake()
		if migrationReconciler != nil {
			migrationReconciler.Wake()
		}
	}
	migrationReservations := func(ctx context.Context) ([]vmmigration.TargetReservation, error) {
		if migrationManager == nil {
			return nil, nil
		}
		return migrationManager.TargetReservations(ctx)
	}
	hostDependencies := host.Dependencies{
		Mesh: mesh, WireGuard: wireGuardManager, Images: stores.Images,
		VirtualMachines: virtualMachineManager, Storage: stores.Pool,
		MigrationReservations: migrationReservations, Wake: notifyReconcilers,
	}
	if unicastManager != nil {
		hostDependencies.Unicast = unicastManager
	}
	hostService, err := host.NewService(hostDependencies)
	if err != nil {
		return fmt.Errorf("configure host service: %w", err)
	}
	migrationCapacity := func(ctx context.Context) (vmmigration.AvailableCapacity, error) {
		capacity, err := hostService.Capacity(ctx)
		if err != nil {
			return vmmigration.AvailableCapacity{}, err
		}
		return vmmigration.AvailableCapacity{
			MemoryMiB:  capacity.AvailableMemoryMiB,
			StorageMiB: capacity.AvailableStorageMiB,
		}, nil
	}
	migrationManager, err = vmmigration.NewVMMigration(
		virtualMachineManager,
		vmmigration.NewSourceClient(0, options.migration.transferPort),
		storage.NewMigrationTransfer(stores.Pool),
		migrationCapacity,
		vmmigration.MigrationSettings{FinalDeltaMiB: options.migration.finalDeltaMiB},
		logger,
	)
	if err != nil {
		return fmt.Errorf("configure migration manager: %w", err)
	}
	// The manager pauses mutation and reconciliation for a migrating VM through
	// this guard, so it never imports the migration package.
	virtualMachineManager.SetMigrationGuard(migrationManager)
	migrationReconciler = reconciler.NewMigrationReconciler(
		migrationManager,
		reconcileInterval,
		reconciler.MigrationConfig{Logger: logger},
	)
	daemon.OwnMigrations(migrationManager)
	migrationListener, err := vmmigration.Listen(transferListenAddress(options.listen, options.migration.transferPort), migrationManager, logger)
	if err != nil {
		return fmt.Errorf("start migration transfer listener: %w", err)
	}
	daemon.OwnMigrationListener(migrationListener)
	server, err := api.New(api.Config{AuthTokenHash: options.authTokenHash, Logger: logger}, api.Dependencies{
		VirtualMachineManager: virtualMachineManager,
		MigrationManager:      migrationManager,
		SnapshotStore:         stores.Snapshots,
		WakeReconciler:        notifyReconcilers,
		HostService:           hostService,
		SerialBroker:          serialBroker,
	})
	if err != nil {
		return fmt.Errorf("configure API: %w", err)
	}

	// Start workers and serve the API.
	listener, err := listen(options.listen)
	if err != nil {
		return fmt.Errorf("listen %s: %w", options.listen, err)
	}
	logger.Info("metald listening", "version", version, "address", options.listen)
	server.Listener = listener
	daemon.StartWorker(virtualMachineReconciler.Run)
	daemon.StartWorker(imageReconciler.Run)
	daemon.StartWorker(migrationReconciler.Run)
	if trafficMonitor != nil {
		daemon.StartTrafficListener(trafficMonitor.Events(), virtualMachineManager.RestoreAfterTraffic)
	}
	return daemon.Serve(server)
}
