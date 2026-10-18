package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/frappe/atlas/metal/internal/network/traffic"
)

const gracefulShutdownTimeout = 30 * time.Second

type daemonHTTPServer interface {
	Start(address string) error
	Shutdown(context.Context) error
}

type snapshotUploadOwner interface {
	Shutdown(context.Context) error
}

// migrationShutdownOwner stops transfers at shutdown.
type migrationShutdownOwner interface {
	Shutdown(context.Context) error
}

type serialBroker interface {
	Shutdown()
}

type systemdConnection interface {
	Close()
}

// trafficMonitorResource owns the eBPF links, programs, and maps.
type trafficMonitorResource interface {
	Close() error
}

type daemon struct {
	context          context.Context
	cancel           context.CancelFunc
	logger           *slog.Logger
	snapshotUploads  snapshotUploadOwner
	serialBroker     serialBroker
	systemd          systemdConnection
	trafficMonitor   trafficMonitorResource
	workers          sync.WaitGroup
	httpServers      []daemonHTTPServer
	httpServerErrors chan error
	httpServerExits  int
	migrations       migrationShutdownOwner
}

// OwnMigrations makes the daemon stop migration transfers at shutdown.
func (daemon *daemon) OwnMigrations(migrations migrationShutdownOwner) {
	daemon.migrations = migrations
}

func newDaemon(
	daemonContext context.Context,
	cancel context.CancelFunc,
	logger *slog.Logger,
	snapshotUploads snapshotUploadOwner,
	serialBroker serialBroker,
	systemd systemdConnection,
) *daemon {
	return &daemon{
		context:         daemonContext,
		cancel:          cancel,
		logger:          logger,
		snapshotUploads: snapshotUploads,
		serialBroker:    serialBroker,
		systemd:         systemd,
	}
}

// OwnTrafficMonitor makes the daemon the owner of the traffic monitor.
func (daemon *daemon) OwnTrafficMonitor(monitor trafficMonitorResource) {
	daemon.trafficMonitor = monitor
}

// StartWorker starts one daemon-owned background worker.
func (daemon *daemon) StartWorker(run func(context.Context)) {
	daemon.workers.Add(1)
	go func() {
		defer daemon.workers.Done()
		run(daemon.context)
	}()
}

// StartTrafficListener dispatches each traffic event without blocking the listener.
func (daemon *daemon) StartTrafficListener(events <-chan traffic.Event, restore func(context.Context, traffic.Event) error) {
	daemon.StartWorker(func(ctx context.Context) {
		for {
			select {
			case <-ctx.Done():
				return
			case event, open := <-events:
				if !open {
					return
				}
				trafficEvent := event
				daemon.StartWorker(func(ctx context.Context) {
					if err := restore(ctx, trafficEvent); err != nil {
						daemon.logger.Error("traffic restoration failed", "virtual_machine_id", trafficEvent.Target.VirtualMachineID, "error", err)
					}
				})
			}
		}
	})
}

// Serve runs the HTTP server until it stops or the daemon receives a signal.
func (daemon *daemon) Serve(servers ...daemonHTTPServer) error {
	daemon.httpServers = servers
	daemon.httpServerErrors = make(chan error, len(servers))
	for _, server := range servers {
		go func() { daemon.httpServerErrors <- server.Start("") }()
	}

	signalContext, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	select {
	case err := <-daemon.httpServerErrors:
		daemon.httpServerExits++
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	case <-signalContext.Done():
		daemon.logger.Info("metald shutdown requested")
		return nil
	}
}

// Shutdown stops daemon work without stopping guest virtual machines.
func (daemon *daemon) Shutdown(shutdownContext context.Context) error {
	var shutdownErrors []error
	for _, server := range daemon.httpServers {
		if err := server.Shutdown(shutdownContext); err != nil && err != http.ErrServerClosed {
			shutdownErrors = append(shutdownErrors, fmt.Errorf("shut down HTTP server: %w", err))
		}
	}

	daemon.cancel()
	if err := waitForGroup(shutdownContext, &daemon.workers); err != nil {
		shutdownErrors = append(shutdownErrors, fmt.Errorf("wait for reconcilers: %w", err))
	}
	// Close eBPF resources only after workers stop, so no worker reads a closed map.
	if daemon.trafficMonitor != nil {
		if err := daemon.trafficMonitor.Close(); err != nil {
			shutdownErrors = append(shutdownErrors, fmt.Errorf("close traffic monitor: %w", err))
		}
	}
	if daemon.migrations != nil {
		if err := daemon.migrations.Shutdown(shutdownContext); err != nil {
			shutdownErrors = append(shutdownErrors, err)
		}
	}
	if err := daemon.snapshotUploads.Shutdown(shutdownContext); err != nil {
		shutdownErrors = append(shutdownErrors, err)
	}
	daemon.serialBroker.Shutdown()
	daemon.systemd.Close()

	for range len(daemon.httpServers) - daemon.httpServerExits {
		select {
		case err := <-daemon.httpServerErrors:
			if err != nil && err != http.ErrServerClosed {
				shutdownErrors = append(shutdownErrors, fmt.Errorf("serve HTTP: %w", err))
			}
		case <-shutdownContext.Done():
			shutdownErrors = append(shutdownErrors, fmt.Errorf("wait for HTTP server: %w", shutdownContext.Err()))
		}
	}
	daemon.logger.Info("metald stopped")
	return errors.Join(shutdownErrors...)
}

func waitForGroup(waitContext context.Context, workers *sync.WaitGroup) error {
	finished := make(chan struct{})
	go func() {
		workers.Wait()
		close(finished)
	}()
	select {
	case <-finished:
		return nil
	case <-waitContext.Done():
		return waitContext.Err()
	}
}
