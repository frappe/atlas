package api

import (
	"github.com/labstack/echo/v4"
)

// registerRoutes binds handlers. PUT mutations are idempotent; POST is for
// actions and resource creation. The TLS listener authenticates the caller.
func (s *Server) registerRoutes(router *echo.Echo) {
	router.GET("/health", s.checkHealth)
	router.GET("/docs", s.showDocumentation)
	router.GET("/docs/swagger.json", s.getOpenAPISpecification)

	versionOneRoutes := router.Group("/v1")
	versionOneRoutes.POST("/sync", s.exchangeControllerState)

	virtualMachineRoutes := versionOneRoutes.Group("/vms")
	virtualMachineRoutes.GET("", s.listVirtualMachines)
	virtualMachineRoutes.PUT("/:id", s.createVirtualMachine)
	virtualMachineRoutes.GET("/:id", s.getVirtualMachine)
	virtualMachineRoutes.PUT("/:id/power", s.setVirtualMachinePowerState)
	virtualMachineRoutes.POST("/:id/restart", s.restartVirtualMachine)
	virtualMachineRoutes.PUT("/:id/compute", s.setVirtualMachineCompute)
	virtualMachineRoutes.PUT("/:id/disk", s.setVirtualMachineDisk)
	virtualMachineRoutes.PUT("/:id/resize", s.resizeVirtualMachine)
	virtualMachineRoutes.PUT("/:id/network", s.setVirtualMachineNetwork)
	virtualMachineRoutes.PUT("/:id/ssh-keys", s.replaceVirtualMachineSSHKeys)
	virtualMachineRoutes.PUT("/:id/metadata", s.replaceVirtualMachineMetadata)
	virtualMachineRoutes.DELETE("/:id", s.deleteVirtualMachine)
	virtualMachineRoutes.POST("/:id/snapshots", s.createVirtualMachineSnapshot)
	virtualMachineRoutes.GET("/:id/console", s.getVirtualMachineConsole)

	snapshotRoutes := versionOneRoutes.Group("/snapshots")
	snapshotRoutes.POST("/:id/upload", s.uploadSnapshot)
	snapshotRoutes.GET("/:id", s.getSnapshot)
	snapshotRoutes.DELETE("/:id", s.deleteSnapshot)

	migrationRoutes := versionOneRoutes.Group("/migrations")
	migrationRoutes.PUT("/:id", s.createMigration)
	migrationRoutes.GET("/:id", s.getMigration)
	migrationRoutes.POST("/:id/abort", s.abortMigration)
	migrationRoutes.POST("/:id/finish", s.finishMigration)
}

// registerCoordinationRoutes binds the API that Metal nodes call with mutual TLS.
func (s *Server) registerCoordinationRoutes(router *echo.Echo) {
	sourceRoutes := router.Group("/v1/migrations")
	sourceRoutes.PUT("/:id/source", s.prepareMigrationSource)
	sourceRoutes.POST("/:id/snapshot", s.createMigrationSnapshot)
	sourceRoutes.POST("/:id/stream", s.startMigrationStream)
	sourceRoutes.POST("/:id/stop", s.stopMigrationSource)
	sourceRoutes.POST("/:id/start", s.startMigrationSource)
	sourceRoutes.POST("/:id/destroy", s.destroyMigrationSource)
	sourceRoutes.DELETE("/:id", s.deleteMigrationSource)
}
