package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

// @Summary	List images
// @Tags		images
// @Produce	json
// @Success	200	{object}	imageListResponse
// @Router		/images [get]
func (s *Server) listImages(c echo.Context) error {
	imgs, err := s.driver.Images(c.Request().Context())
	if err != nil {
		return err
	}
	out := make([]imageResponse, 0, len(imgs))
	for _, im := range imgs {
		out = append(out, toImage(im))
	}
	return c.JSON(http.StatusOK, imageListResponse{Images: out})
}

// @Summary	Delete an image
// @Tags		images
// @Param		ref	path	string	true	"Image reference"
// @Success	204	"No content"
// @Failure	400	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/images/{ref} [delete]
func (s *Server) deleteImage(c echo.Context) error {
	ref := c.Param("ref")
	if !validImageReference(ref) {
		return badRequest("invalid image reference")
	}
	if err := s.driver.DeleteImage(c.Request().Context(), ref); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}
