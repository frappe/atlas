package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Set virtual machine compute resources and idle shutdown
// @Description	Store the complete CPU and memory shape. The virtual machine must be stopped.
// @ID			setVirtualMachineCompute
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Security	BearerAuth
// @Param		id		path		string			true	"Virtual machine identifier"
// @Param		request	body		computeRequest	true	"Complete compute specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/compute [put]
func (s *Server) setVirtualMachineCompute(c echo.Context) error {
	var request computeRequest
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
	if err := s.validateComputeCapacity(c, request, virtualMachine); err != nil {
		return err
	}
	if err := s.virtualMachineManager.SetCompute(c.Request().Context(), virtualMachine.ID, vm.Compute{
		CPUMillicores:         request.CPUMillicores,
		MemoryMiB:             request.MemoryMiB,
		SleepAfterIdleSeconds: request.SleepAfterIdleSeconds,
	}); err != nil {
		return err
	}

	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}

// validateComputeCapacity rejects a request the host cannot satisfy. Only the
// increase is checked, because the VM already holds what it reserves. Virtual
// CPU entitlement is oversubscribed and never limits a request.
func (s *Server) validateComputeCapacity(c echo.Context, request computeRequest, current vm.Information) error {
	capacity, err := s.hostService.Capacity(c.Request().Context())
	if err != nil {
		return err
	}
	if needsMoreThanAvailable(request.MemoryMiB, current.MemoryMiB, capacity.AvailableMemoryMiB) {
		return newAPIError(http.StatusConflict, insufficientCapacityCode, "not enough host memory capacity")
	}
	return nil
}

// insufficientCapacityCode tells Atlas to move the VM to another host.
const insufficientCapacityCode = "insufficient_capacity"

// needsMoreThanAvailable reports whether the increase exceeds free capacity.
func needsMoreThanAvailable(requested, current, available int) bool {
	return requested-current > available
}
