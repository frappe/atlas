package api

import (
	"net/http"
	"regexp"

	"github.com/labstack/echo/v4"
)

func (s *Server) listSnapshots(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	snaps, err := m.DiskSnapshots(c.Request().Context())
	if err != nil {
		return err
	}
	out := make([]snapResp, 0, len(snaps))
	for _, sn := range snaps {
		out = append(out, toSnap(m.ID(), sn))
	}
	return c.JSON(http.StatusOK, echo.Map{"snapshots": out})
}

func (s *Server) createSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body snapReq
	if err := c.Bind(&body); err != nil {
		return badRequest(err.Error())
	}
	if !validSnapName(body.Name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.DiskSnapshot(c.Request().Context(), body.Name); err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, echo.Map{"name": body.Name})
}

func (s *Server) deleteSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	name := c.Param("name")
	if !validSnapName(name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.DeleteDiskSnapshot(c.Request().Context(), name); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

func (s *Server) restoreSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	name := c.Param("name")
	if !validSnapName(name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.RestoreDiskSnapshot(c.Request().Context(), name); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// snapNameRe matches ZFS-legal snapshot names (the part after '@').
var snapNameRe = regexp.MustCompile(`^[A-Za-z0-9_.:-]+$`)

func validSnapName(s string) bool { return snapNameRe.MatchString(s) }
