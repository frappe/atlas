package api

import (
	"net/http"

	"github.com/labstack/echo/v5"
)

// @Summary Reset virtual machine CPU burst
// @Description Store the current time as the CPU burst reset time. The sliding window average ignores older CPU samples. The current CPU limit stays.
// @ID resetVirtualMachineCPUBurst
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Success 200 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/cpu-burst/reset [post]
func (s *Server) resetVirtualMachineCPUBurst(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	if err := s.virtualMachineManager.ResetCPUBurst(c.Request().Context(), identifier); err != nil {
		return err
	}
	return s.respondWithCurrentVirtualMachine(c, http.StatusOK)
}
