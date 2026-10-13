package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

// Versions names the builds that run on this host. An empty value means that
// metald could not read it.
type Versions struct {
	MetaldVersion      string `json:"metald_version"`
	MeshVersion        string `json:"wg_mesh_version"`
	FirecrackerVersion string `json:"firecracker_version"`
}

// @Summary		Get host versions
// @Description	Return the metald, Atlas WG Mesh, and Firecracker versions on this host. An empty value means that metald could not read it.
// @ID			getVersions
// @Tags		Host synchronization
// @Produce	json
// @Success	200	{object}	Versions
// @Failure	401	{object}	errorResponse
// @Router		/v1/versions [get]
func (s *Server) getVersions(c echo.Context) error {
	return c.JSON(http.StatusOK, s.versions)
}
