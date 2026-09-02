package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

func (s *Server) listImages(c echo.Context) error {
	imgs, err := s.driver.Images(c.Request().Context())
	if err != nil {
		return err
	}
	out := make([]imageResp, 0, len(imgs))
	for _, im := range imgs {
		out = append(out, toImage(im))
	}
	return c.JSON(http.StatusOK, echo.Map{"images": out})
}

func (s *Server) deleteImage(c echo.Context) error {
	ref := c.Param("ref")
	if !validImageRef(ref) {
		return badRequest("invalid image ref")
	}
	if err := s.driver.DeleteImage(c.Request().Context(), ref); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}
