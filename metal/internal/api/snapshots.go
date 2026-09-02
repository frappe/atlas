package api

import (
	"net/http"
	"regexp"

	"github.com/labstack/echo/v4"
)

// @Summary	List the snapshots of a virtual machine
// @Tags		snapshots
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	snapshotListResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/snapshots [get]
func (s *Server) listSnapshots(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	snaps, err := m.Snapshots(c.Request().Context())
	if err != nil {
		return err
	}
	out := make([]snapshotResponse, 0, len(snaps))
	for _, sn := range snaps {
		out = append(out, toSnapshot(m.ID(), sn))
	}
	return c.JSON(http.StatusOK, snapshotListResponse{Snapshots: out})
}

// @Summary	Create a snapshot
// @Tags		snapshots
// @Accept		json
// @Produce	json
// @Param		id		path		string	true	"Virtual machine identifier"
// @Param		body	body		snapshotRequest	true	"Snapshot name and whether to capture memory"
// @Success	201		{object}	snapshotCreatedResponse
// @Failure	400		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Router		/vms/{id}/snapshots [post]
func (s *Server) createSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	var body snapshotRequest
	if err := c.Bind(&body); err != nil {
		return badRequest(err.Error())
	}
	if !validSnapshotName(body.Name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.Snapshot(c.Request().Context(), body.Name, body.Memory); err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, snapshotCreatedResponse{Name: body.Name, Memory: body.Memory})
}

// @Summary	Delete a snapshot
// @Tags		snapshots
// @Param		id		path	string	true	"Virtual machine identifier"
// @Param		name	path	string	true	"Snapshot name"
// @Success	204		"No content"
// @Failure	404		{object}	errorResponse
// @Router		/vms/{id}/snapshots/{name} [delete]
func (s *Server) deleteSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	name := c.Param("name")
	if !validSnapshotName(name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.DeleteSnapshot(c.Request().Context(), name); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// @Summary		Restore a snapshot
// @Description	The virtual machine must be stopped. The disk rolls back to the snapshot.
// @Tags			snapshots
// @Param			id		path	string	true	"Virtual machine identifier"
// @Param			name	path	string	true	"Snapshot name"
// @Success		204		"No content"
// @Failure		404		{object}	errorResponse
// @Failure		409		{object}	errorResponse
// @Router			/vms/{id}/snapshots/{name}/restore [post]
func (s *Server) restoreSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	name := c.Param("name")
	if !validSnapshotName(name) {
		return badRequest("invalid snapshot name")
	}
	if err := m.RestoreSnapshot(c.Request().Context(), name); err != nil {
		return err
	}
	return s.respond(c, http.StatusOK, m)
}

// @Summary		Promote a snapshot to an image
// @Tags			snapshots
// @Accept			json
// @Produce		json
// @Param			id		path		string		true	"Virtual machine identifier"
// @Param			name	path		string		true	"Snapshot name"
// @Param			body	body		promoteRequest	true	"New image reference"
// @Success		201		{object}	imageCreatedResponse
// @Failure		400		{object}	errorResponse
// @Failure		404		{object}	errorResponse
// @Failure		409		{object}	errorResponse
// @Router			/vms/{id}/snapshots/{name}/promote [post]
func (s *Server) promoteSnapshot(c echo.Context) error {
	m, err := s.load(c)
	if err != nil {
		return err
	}
	name := c.Param("name")
	if !validSnapshotName(name) {
		return badRequest("invalid snapshot name")
	}
	var body promoteRequest
	if err := c.Bind(&body); err != nil {
		return badRequest(err.Error())
	}
	if !validImageReference(body.Image) {
		return badRequest("invalid image reference")
	}
	if err := m.Promote(c.Request().Context(), name, body.Image); err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, imageCreatedResponse{Ref: body.Image})
}

// snapshotNamePattern matches valid ZFS snapshot names after '@'.
var snapshotNamePattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]+$`)

func validSnapshotName(s string) bool { return snapshotNamePattern.MatchString(s) }

// imageReferencePattern matches a valid image reference. The reference becomes
// a ZFS dataset name, so it must start with a letter or number and use only
// letters, numbers, underscores, periods, and hyphens.
var imageReferencePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

func validImageReference(s string) bool { return imageReferencePattern.MatchString(s) }
