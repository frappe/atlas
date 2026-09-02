// Package api serves the Metal HTTP application programming interface over the
// driver. Implemented endpoints are wired to the driver. The console endpoint
// is specified but not implemented and returns status 501.
package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

type Server struct {
	driver vm.VMDriver
}

// New builds the echo router bound to driver.
func New(driver vm.VMDriver) *echo.Echo {
	s := &Server{driver: driver}
	e := echo.New()
	e.HideBanner = true
	e.HTTPErrorHandler = errorHandler

	e.POST("/vms", s.create)
	e.GET("/vms", s.list)
	e.GET("/vms/:id", s.get)
	e.POST("/vms/:id/start", s.start)
	e.POST("/vms/:id/stop", s.stop)
	e.POST("/vms/:id/pause", s.pause)
	e.POST("/vms/:id/resume", s.resume)
	e.DELETE("/vms/:id", s.destroy)

	e.GET("/vms/:id/snapshots", s.listSnapshots)
	e.POST("/vms/:id/snapshots", s.createSnapshot)
	e.DELETE("/vms/:id/snapshots/:name", s.deleteSnapshot)
	e.POST("/vms/:id/snapshots/:name/restore", s.restoreSnapshot)
	e.POST("/vms/:id/snapshots/:name/promote", s.promoteSnapshot)

	e.GET("/images", s.listImages)
	e.DELETE("/images/:ref", s.deleteImage)

	e.POST("/vms/:id/resize", s.resize)

	// Specified but not implemented yet.
	e.GET("/vms/:id/console", notImplemented)

	e.GET("/health", s.health)

	e.GET("/docs", s.docs)
	e.GET("/docs/swagger.json", s.specificationJSON)
	return e
}

// @Summary		Liveness check
// @Description	It answers while the process runs. It checks no dependency.
// @Tags			health
// @Success		200	"No content"
// @Router			/health [get]
func (s *Server) health(c echo.Context) error {
	return c.NoContent(http.StatusOK)
}

// @Summary	Create and start a virtual machine
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		body	body		createRequest	true	"Virtual machine specification"
// @Success	201		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Router		/vms [post]
func (s *Server) create(c echo.Context) error {
	var req createRequest
	if err := c.Bind(&req); err != nil {
		return badRequest(err.Error())
	}
	ctx := c.Request().Context()
	m, err := s.driver.Create(ctx, req.spec())
	if err != nil {
		return err
	}
	if err := m.Start(ctx); err != nil {
		return err
	}
	return s.respond(c, http.StatusCreated, m)
}

// @Summary	List virtual machines
// @Tags		vms
// @Produce	json
// @Success	200	{object}	virtualMachineListResponse
// @Router		/vms [get]
func (s *Server) list(c echo.Context) error {
	ctx := c.Request().Context()
	vms, err := s.driver.List(ctx)
	if err != nil {
		return err
	}
	out := make([]virtualMachineResponse, 0, len(vms))
	for _, m := range vms {
		info, err := m.Info(ctx)
		if err != nil {
			continue
		}
		out = append(out, toVirtualMachine(info))
	}
	return c.JSON(http.StatusOK, virtualMachineListResponse{VMs: out})
}

// @Summary	Get a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id} [get]
func (s *Server) get(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary	Start a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/start [post]
func (s *Server) start(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	if err := m.Start(c.Request().Context()); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary	Stop a virtual machine
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string	true	"Virtual machine identifier"
// @Param		body	body		stopRequest	false	"Set force to stop immediately"
// @Success	200		{object}	virtualMachineResponse
// @Failure	404		{object}	errorResponse
// @Router		/vms/{id}/stop [post]
func (s *Server) stop(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body stopRequest
	_ = c.Bind(&body)
	if err := m.Stop(c.Request().Context(), body.Force); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary	Pause a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/pause [post]
func (s *Server) pause(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	if err := m.Pause(c.Request().Context()); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary	Resume a paused virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/resume [post]
func (s *Server) resume(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	if err := m.Resume(c.Request().Context()); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary	Destroy a virtual machine
// @Tags		vms
// @Param		id	path	string	true	"Virtual machine identifier"
// @Success	204	"No content"
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id} [delete]
func (s *Server) destroy(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	if err := m.Destroy(c.Request().Context()); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// resize currently grows only the disk. Processor and memory changes are not
// yet supported.
//
//	@Summary		Resize a virtual machine disk
//	@Description	disk_mib grows only. vcpus and mem_mib are not implemented.
//	@Tags			vms
//	@Accept			json
//	@Produce		json
//	@Param			id		path		string		true	"Virtual machine identifier"
//	@Param			body	body		resizeRequest	true	"New size"
//	@Success		200		{object}	virtualMachineResponse
//	@Failure		400		{object}	errorResponse
//	@Failure		409		{object}	errorResponse
//	@Failure		501		{object}	errorResponse
//	@Router			/vms/{id}/resize [post]
func (s *Server) resize(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body resizeRequest
	if err := c.Bind(&body); err != nil {
		return badRequest(err.Error())
	}
	if body.VCPUs != nil || body.MemMiB != nil {
		return echo.NewHTTPError(http.StatusNotImplemented, "processor and memory resize not yet supported")
	}
	if body.DiskMiB == nil {
		return badRequest("disk_mib is required")
	}
	if err := m.Resize(c.Request().Context(), *body.DiskMiB); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

func (s *Server) load(c echo.Context) (vm.VM, error) {
	return s.driver.Load(c.Request().Context(), c.Param("id"))
}

// respond fetches live Info for m and writes it as the virtual machine resource.
func (s *Server) respond(c echo.Context, status int, m vm.VM) error {
	info, err := m.Info(c.Request().Context())
	if err != nil {
		return err
	}
	return c.JSON(status, toVirtualMachine(info))
}
