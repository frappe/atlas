package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Set virtual machine disk resources
// @Description	Store the complete disk size and rate limits. Metal does not accept a smaller disk size.
// @ID			setVirtualMachineDisk
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Security	BearerAuth
// @Param		id		path	string		true	"Virtual machine identifier"
// @Param		request	body	diskRequest	true	"Complete disk specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/disk [put]
func (s *Server) setVirtualMachineDisk(c echo.Context) error {
	var request diskRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	if err := s.validateDiskCapacity(c, request, virtualMachine); err != nil {
		return err
	}
	if err := s.virtualMachineManager.SetDisk(
		c.Request().Context(),
		virtualMachine.ID,
		request.SizeMiB,
		request.specification(),
	); err != nil {
		return err
	}

	s.wakeReconciler()

	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}

// validateDiskCapacity rejects growth the pool cannot hold. Only the increase is
// checked, because the VM already holds what it reserves.
func (s *Server) validateDiskCapacity(c echo.Context, request diskRequest, current vm.Information) error {
	capacity, err := s.hostService.Capacity(c.Request().Context())
	if err != nil {
		return err
	}
	if needsMoreThanAvailable(request.SizeMiB, current.DiskMiB, capacity.AvailableStorageMiB) {
		return newAPIError(http.StatusConflict, insufficientCapacityCode, "not enough host storage capacity")
	}

	return nil
}
