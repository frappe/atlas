// Package api serves the metal HTTP API (see docs/api.md) over the driver.
// Implemented endpoints are wired to the driver; specced-but-unbuilt ones
// (console) return 501.
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
	e.DELETE("/vms/:id", s.destroy)

	e.GET("/vms/:id/snapshots", s.listSnapshots)
	e.POST("/vms/:id/snapshots", s.createSnapshot)
	e.DELETE("/vms/:id/snapshots/:name", s.deleteSnapshot)
	e.POST("/vms/:id/snapshots/:name/restore", s.restoreSnapshot)

	e.POST("/vms/:id/resize", s.resize)

	// Specced but not implemented yet.
	e.GET("/vms/:id/console", notImplemented)

	e.GET("/health", func(c echo.Context) error { return c.NoContent(http.StatusOK) })
	return e
}

func (s *Server) create(c echo.Context) error {
	var req createReq
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

func (s *Server) list(c echo.Context) error {
	ctx := c.Request().Context()
	vms, err := s.driver.List(ctx)
	if err != nil {
		return err
	}
	out := make([]vmResp, 0, len(vms))
	for _, m := range vms {
		info, err := m.Info(ctx)
		if err != nil {
			continue
		}
		out = append(out, toVM(info))
	}
	return c.JSON(http.StatusOK, echo.Map{"vms": out})
}

func (s *Server) get(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

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

func (s *Server) stop(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body stopReq
	_ = c.Bind(&body)
	if err := m.Stop(c.Request().Context(), body.Force); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

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

// resize currently grows only the disk. CPU/mem changes are not yet supported.
func (s *Server) resize(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body resizeReq
	if err := c.Bind(&body); err != nil {
		return badRequest(err.Error())
	}
	if body.VCPUs != nil || body.MemMiB != nil {
		return echo.NewHTTPError(http.StatusNotImplemented, "cpu/mem resize not yet supported")
	}
	if body.DiskMiB == nil {
		return badRequest("disk_mib required")
	}
	if err := m.Resize(c.Request().Context(), *body.DiskMiB); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

func (s *Server) load(c echo.Context) (vm.VM, error) {
	return s.driver.Load(c.Request().Context(), c.Param("id"))
}

// respond fetches live Info for m and writes it as the VM resource.
func (s *Server) respond(c echo.Context, status int, m vm.VM) error {
	info, err := m.Info(c.Request().Context())
	if err != nil {
		return err
	}
	return c.JSON(status, toVM(info))
}
