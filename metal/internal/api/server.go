// Package api serves the Metal HTTP API.
package api

import (
	"context"
	"fmt"
	"io"
	"log/slog"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
	"github.com/frappe/atlas/metal/internal/vm/migration"
)

// Config contains HTTP server configuration. The TLS listener authenticates the caller.
type Config struct {
	Logger *slog.Logger
}

// SnapshotStore uploads and removes local image staging snapshots.
type SnapshotStore interface {
	StartUpload(ctx context.Context, snapshotID string, request storage.SnapshotUploadRequest) error
	UploadStatus(ctx context.Context, snapshotID string) (storage.SnapshotUploadStatus, error)
	DeleteSnapshot(ctx context.Context, snapshotID string) error
}

// HostService synchronizes controller-owned host state and reports capacity.
type HostService interface {
	Synchronize(context.Context, host.DesiredState) (host.SyncResult, error)
	Capacity(context.Context) (host.Capacity, error)
}

// SerialBroker streams a virtual machine serial console to one viewer.
type SerialBroker interface {
	Attach(ctx context.Context, id string, client io.ReadWriter, resize <-chan console.Winsize) error
}

// VirtualMachineManager owns virtual machine state and operations.
type VirtualMachineManager interface {
	Create(context.Context, string, vm.Specification) (vm.Information, error)
	Information(context.Context, string) (vm.Information, error)
	List(context.Context) ([]vm.Information, error)
	SetPowerState(context.Context, string, vm.State) error
	RequestRestart(context.Context, string) error
	SetCompute(context.Context, string, vm.Compute) error
	SetDisk(context.Context, string, int, vm.Disk) error
	Resize(context.Context, string, vm.Compute, int) error
	LockCapacity() func()
	SetNetwork(context.Context, string, vm.NetworkConfiguration) error
	ReplaceSSHKeys(context.Context, string, []string) (bool, error)
	ReplaceMetadata(context.Context, string, map[string]string) (bool, error)
	Delete(context.Context, string) error
	CreateSnapshot(context.Context, string) (vm.StagedSnapshot, error)
	ConnectSSH(context.Context, string) (vm.SSHConnection, error)
}

// MigrationManager owns this host's migration records and reservations.
type MigrationManager interface {
	CreateDestination(ctx context.Context, migrationID, virtualMachineID, source string, resize *migration.Resize) (migration.DestinationProgress, error)
	DestinationStatus(ctx context.Context, migrationID string) (migration.DestinationProgress, error)
	RequestFinish(ctx context.Context, migrationID string) error
	AbortDestination(ctx context.Context, migrationID string) error
	LockSource(ctx context.Context, migrationID, virtualMachineID string) (migration.SourceDescription, error)
	NextSourceSnapshot(ctx context.Context, migrationID, virtualMachineID string, receivedSequence int) (migration.SourceSnapshot, error)
	StartSourceStream(ctx context.Context, migrationID, virtualMachineID string, sequence int, resumeToken string, throughputMiBps int) error
	StopSource(ctx context.Context, migrationID, virtualMachineID string, receivedSequence int) (migration.SourceSnapshot, error)
	StartSourceRollback(ctx context.Context, migrationID, virtualMachineID string) error
	DestroySource(ctx context.Context, migrationID, virtualMachineID string) error
	UnlockSource(ctx context.Context, migrationID, virtualMachineID string) error
}

// Dependencies contains services used by the HTTP handlers.
type Dependencies struct {
	VirtualMachineManager VirtualMachineManager
	MigrationManager      MigrationManager
	SnapshotStore         SnapshotStore
	WakeReconciler        func()
	HostService           HostService
	SerialBroker          SerialBroker
}

// Server owns the HTTP handlers and their dependencies.
type Server struct {
	virtualMachineManager VirtualMachineManager
	migrationManager      MigrationManager
	snapshotStore         SnapshotStore
	wakeReconciler        func()
	hostService           HostService
	serialBroker          SerialBroker
	logger                *slog.Logger
}

// New builds the HTTP router from explicit configuration and dependencies.
func New(configuration Config, dependencies Dependencies) (*echo.Echo, error) {
	if err := validateServerConfiguration(configuration, dependencies); err != nil {
		return nil, err
	}

	server := &Server{
		virtualMachineManager: dependencies.VirtualMachineManager,
		migrationManager:      dependencies.MigrationManager,
		snapshotStore:         dependencies.SnapshotStore,
		wakeReconciler:        dependencies.WakeReconciler,
		hostService:           dependencies.HostService,
		serialBroker:          dependencies.SerialBroker,
	}
	if configuration.Logger == nil {
		configuration.Logger = slog.Default()
	}
	server.logger = configuration.Logger

	router := echo.New()
	router.HideBanner = true
	router.HTTPErrorHandler = errorHandler

	// Correlation runs first, so every log line and error carries the same IDs.
	router.Use(correlationMiddleware)
	router.Use(server.logRequest)
	server.registerRoutes(router)

	return router, nil
}

// NewCoordination builds the mutual-TLS router used between Metal nodes.
// The listener enforces the client certificate before a request reaches it.
func NewCoordination(logger *slog.Logger, migrationManager MigrationManager) (*echo.Echo, error) {
	if migrationManager == nil {
		return nil, fmt.Errorf("migration manager is required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	server := &Server{migrationManager: migrationManager, logger: logger}
	router := echo.New()
	router.HideBanner = true
	router.HTTPErrorHandler = errorHandler
	router.Use(correlationMiddleware)
	router.Use(server.logRequest)
	server.registerCoordinationRoutes(router)
	return router, nil
}

// logRequest records each request outcome.
func (s *Server) logRequest(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		c.Set("logger", s.logger)

		err := next(c)
		status := c.Response().Status
		if err != nil {
			status = publicAPIError(err).status
		}

		s.logger.Info("API request",
			"method", c.Request().Method, "path", c.Path(), "status", status,
			"request_id", requestID(c.Request().Context()),
			"operation_id", operationID(c.Request().Context()),
		)

		return err
	}
}

// validateServerConfiguration rejects unsafe server configuration.
func validateServerConfiguration(_ Config, dependencies Dependencies) error {
	if dependencies.VirtualMachineManager == nil || dependencies.MigrationManager == nil || dependencies.SnapshotStore == nil || dependencies.WakeReconciler == nil || dependencies.HostService == nil || dependencies.SerialBroker == nil {
		return fmt.Errorf("API dependencies are required")
	}
	return nil
}
