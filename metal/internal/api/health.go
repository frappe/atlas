package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

// @Summary		Liveness check
// @Description	It answers while the process runs. It checks no dependency.
// @Tags			health
// @Success		200	"No content"
// @Router			/health [get]
func (s *Server) checkHealth(c echo.Context) error {
	return c.NoContent(http.StatusOK)
}
