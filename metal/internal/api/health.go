package api

import (
	"net/http"

	"github.com/labstack/echo/v5"
)

// @Summary		Liveness check
// @Description	Return 200 while the Metal HTTP server can accept requests. This check does not call host services.
// @ID			checkHealth
// @Tags			Health
// @Success		200	"No content"
// @Router			/health [get]
func (s *Server) checkHealth(c *echo.Context) error {
	return c.NoContent(http.StatusOK)
}
