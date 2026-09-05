package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

// @Summary	Update VM disk limits
// @Description	Apply disk throughput and IOPS limits without restarting the VM.
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path	string		true	"Virtual machine identifier"
// @Param		body	body	diskRequest	true	"Disk limits"
// @Success	200	{object}	virtualMachineResponse
// @Failure	400	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/disk [put]
func (s *Server) updateVirtualMachineDiskLimits(c echo.Context) error {
	virtualMachineID := c.Param("id")
	if !validResourceID(virtualMachineID) {
		return badRequest("invalid virtual machine identifier")
	}

	var request diskRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	if err := virtualMachine.UpdateDiskLimits(c.Request().Context(), request.spec()); err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, virtualMachine)
}
