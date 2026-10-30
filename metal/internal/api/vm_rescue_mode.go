package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

// rescueModeRequest selects the image for rescue mode.
type rescueModeRequest struct {
	Image imageRequest `json:"image"`
}

// @Summary Enable virtual machine rescue mode
// @Description Store the rescue image. The next cold boot uses it and attaches the VM disk as the second drive. A repeat replaces the image and its URLs.
// @ID enableVirtualMachineRescueMode
// @Tags Virtual machines
// @Accept json
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param request body rescueModeRequest true "Rescue image"
// @Success 202 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/rescue-mode [put]
func (s *Server) enableVirtualMachineRescueMode(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	var request rescueModeRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.Image.validate(); err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineManager.EnableRescueMode(c.Request().Context(), identifier, request.Image.specification()); err != nil {
		return err
	}
	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}

// @Summary Disable virtual machine rescue mode
// @Description Clear the rescue image. The next cold boot uses the VM disk and removes the rescue disk.
// @ID disableVirtualMachineRescueMode
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Success 202 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/rescue-mode [delete]
func (s *Server) disableVirtualMachineRescueMode(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	if err := s.virtualMachineManager.DisableRescueMode(c.Request().Context(), identifier); err != nil {
		return err
	}
	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}
