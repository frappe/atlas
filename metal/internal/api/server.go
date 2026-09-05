// Package api serves the Metal HTTP API.
package api

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"io"
	"strings"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// Config contains HTTP server configuration.
type Config struct {
	AuthTokenHash string
}

// PrivilegedMesh replaces the Atlas WG Mesh privileged VM whitelist.
type PrivilegedMesh interface {
	ApplyPrivilegedAddresses(ctx context.Context, addresses []string) error
}

// WireGuardManager manages WireGuard peers.
type WireGuardManager interface {
	Apply(ctx context.Context, peers []network.WireGuardPeer) error
}

// CapacityProvider reports storage capacity.
type CapacityProvider interface {
	Capacity(ctx context.Context) (storage.Capacity, error)
}

// SnapshotCreator creates local image staging snapshots.
type SnapshotCreator interface {
	CreateSnapshot(ctx context.Context, virtualMachineID string) (storage.StagedSnapshot, error)
}

// SnapshotStore uploads and removes local image staging snapshots.
type SnapshotStore interface {
	StartUpload(ctx context.Context, snapshotID string, request storage.SnapshotUploadRequest) error
	UploadStatus(ctx context.Context, snapshotID string) (storage.SnapshotUploadStatus, error)
	DeleteSnapshot(ctx context.Context, snapshotID string) error
}

// ImagePolicyStore records the image policies from the controller.
type ImagePolicyStore interface {
	SetImagePolicies(ctx context.Context, images []vm.ImageRef) error
}

// ConsoleBroker streams a virtual machine serial console to one viewer.
type ConsoleBroker interface {
	Attach(ctx context.Context, id string, client io.ReadWriter, resize <-chan console.Winsize) error
}

// SSHConnector opens an interactive SSH session to a virtual machine guest.
type SSHConnector interface {
	DialSSH(ctx context.Context, id string) (vm.SSHConn, error)
}

// Dependencies contains services used by the HTTP handlers.
type Dependencies struct {
	VirtualMachineDriver vm.Driver
	SnapshotCreator      SnapshotCreator
	SnapshotStore        SnapshotStore
	ImagePolicyStore     ImagePolicyStore
	WakeReconciler       func()
	WireGuardManager     WireGuardManager
	Mesh                 PrivilegedMesh
	Storage              CapacityProvider
	ConsoleBroker        ConsoleBroker
	SSHConnector         SSHConnector
}

// Server owns the HTTP handlers and their dependencies.
type Server struct {
	virtualMachineDriver vm.Driver
	snapshotCreator      SnapshotCreator
	snapshotStore        SnapshotStore
	imagePolicyStore     ImagePolicyStore
	wakeReconciler       func()
	wireGuardManager     WireGuardManager
	mesh                 PrivilegedMesh
	storage              CapacityProvider
	consoleBroker        ConsoleBroker
	sshConnector         SSHConnector
	authTokenHash        []byte
}

// New builds the HTTP router from explicit configuration and dependencies.
func New(configuration Config, dependencies Dependencies) (*echo.Echo, error) {
	if err := validateServerConfiguration(configuration, dependencies); err != nil {
		return nil, err
	}

	server := &Server{
		virtualMachineDriver: dependencies.VirtualMachineDriver,
		snapshotCreator:      dependencies.SnapshotCreator,
		snapshotStore:        dependencies.SnapshotStore,
		imagePolicyStore:     dependencies.ImagePolicyStore,
		wakeReconciler:       dependencies.WakeReconciler,
		wireGuardManager:     dependencies.WireGuardManager,
		mesh:                 dependencies.Mesh,
		storage:              dependencies.Storage,
		consoleBroker:        dependencies.ConsoleBroker,
		sshConnector:         dependencies.SSHConnector,
		authTokenHash:        []byte(configuration.AuthTokenHash),
	}

	router := echo.New()
	router.HideBanner = true
	router.HTTPErrorHandler = errorHandler
	router.Use(server.authenticate)
	server.registerRoutes(router)

	return router, nil
}

func validateServerConfiguration(configuration Config, dependencies Dependencies) error {
	if len(configuration.AuthTokenHash) != sha256.Size*2 {
		return fmt.Errorf("API authentication token SHA-256 hash is required")
	}
	if _, err := hex.DecodeString(configuration.AuthTokenHash); err != nil || configuration.AuthTokenHash != strings.ToLower(configuration.AuthTokenHash) {
		return fmt.Errorf("API authentication token SHA-256 hash is invalid")
	}
	if dependencies.VirtualMachineDriver == nil || dependencies.SnapshotCreator == nil || dependencies.SnapshotStore == nil || dependencies.ImagePolicyStore == nil || dependencies.WakeReconciler == nil || dependencies.WireGuardManager == nil || dependencies.Mesh == nil || dependencies.Storage == nil || dependencies.ConsoleBroker == nil || dependencies.SSHConnector == nil {
		return fmt.Errorf("API dependencies are required")
	}
	return nil
}

func (s *Server) authenticate(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		if isPublicPath(c.Path()) {
			return next(c)
		}

		token := c.Request().Header.Get("Authorization")
		const prefix = "Bearer "
		if len(token) <= len(prefix) || token[:len(prefix)] != prefix {
			return unauthorized()
		}

		digest := sha256.Sum256([]byte(token[len(prefix):]))
		if subtle.ConstantTimeCompare(s.authTokenHash, []byte(hex.EncodeToString(digest[:]))) != 1 {
			return unauthorized()
		}
		return next(c)
	}
}

func isPublicPath(path string) bool {
	return path == "/docs" || path == "/docs/swagger.json"
}
