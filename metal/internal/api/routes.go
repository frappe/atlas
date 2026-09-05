package api

import "github.com/labstack/echo/v4"

func (s *Server) registerRoutes(router *echo.Echo) {
	router.GET("/health", s.checkHealth)
	router.POST("/sync", s.exchangeControllerState)

	virtualMachineRoutes := router.Group("/vms")
	virtualMachineRoutes.GET("", s.listVirtualMachines)
	virtualMachineRoutes.PUT("/:id", s.createVirtualMachine)
	virtualMachineRoutes.GET("/:id", s.getVirtualMachine)
	virtualMachineRoutes.PUT("/:id/network", s.updateVirtualMachineNetwork)
	virtualMachineRoutes.PUT("/:id/ssh-keys", s.replaceVirtualMachineSSHKeys)
	virtualMachineRoutes.PUT("/:id/metadata", s.replaceVirtualMachineMetadata)

	actionRoutes := virtualMachineRoutes.Group("/:id/actions")
	actionRoutes.POST("/start", s.startVirtualMachine)
	actionRoutes.POST("/stop", s.stopVirtualMachine)
	actionRoutes.POST("/pause", s.pauseVirtualMachine)
	actionRoutes.POST("/resume", s.resumeVirtualMachine)
	actionRoutes.POST("/reboot", s.rebootVirtualMachine)
	actionRoutes.POST("/terminate", s.terminateVirtualMachine)

	resizeRoutes := virtualMachineRoutes.Group("/:id/resize")
	resizeRoutes.POST("/compute", s.resizeVirtualMachineCompute)
	resizeRoutes.POST("/disk", s.growVirtualMachineDisk)

	virtualMachineRoutes.POST("/:id/snapshots", s.createVirtualMachineSnapshot)

	virtualMachineRoutes.GET("/:id/console", s.getVirtualMachineConsole)

	snapshotRoutes := router.Group("/snapshots")
	snapshotRoutes.POST("/:snapshot_id/upload", s.uploadSnapshot)
	snapshotRoutes.GET("/:snapshot_id", s.getSnapshot)
	snapshotRoutes.DELETE("/:snapshot_id", s.deleteSnapshot)

	router.GET("/docs", s.showDocumentation)
	router.GET("/docs/swagger.json", s.getOpenAPISpecification)
}
