package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Resize VM compute resources
// @Description	The VM must be stopped. The change requests a new boot.
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string				true	"Virtual machine identifier"
// @Param		body	body		computeResizeRequest	true	"New compute size"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Router		/vms/{id}/resize/compute [post]
func (s *Server) resizeVirtualMachineCompute(c echo.Context) error {
	var request computeResizeRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if request.VCPUs <= 0 || request.MemoryMiB <= 0 {
		return badRequest("vcpus and memory_mib must be positive")
	}
	if request.MemoryMiB > maximumMemoryMiB {
		return badRequest("memory_mib is too large")
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}

	ctx := c.Request().Context()
	information, err := virtualMachine.Info(ctx)
	if err != nil {
		return err
	}
	if information.State != vm.StateStopped {
		return echo.NewHTTPError(http.StatusConflict, "stop the virtual machine before changing CPU or memory")
	}

	availableCPUCount, availableMemoryMiB, _, err := s.getComputeCapacity(ctx)
	if err != nil {
		return err
	}
	if needsMoreThanAvailable(request.VCPUs, information.VCPUs, availableCPUCount) {
		return echo.NewHTTPError(http.StatusConflict, "not enough host CPU capacity")
	}
	if needsMoreThanAvailable(request.MemoryMiB, information.MemoryMiB, availableMemoryMiB) {
		return echo.NewHTTPError(http.StatusConflict, "not enough host memory capacity")
	}

	if err := s.virtualMachineDriver.ResizeCompute(ctx, virtualMachine.ID(), request.VCPUs, request.MemoryMiB); err != nil {
		return err
	}

	s.wakeReconciler()
	updatedVirtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusAccepted, updatedVirtualMachine)
}

// @Summary	Grow a VM disk
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string			true	"Virtual machine identifier"
// @Param		body	body		diskResizeRequest	true	"New disk size"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Router		/vms/{id}/resize/disk [post]
func (s *Server) growVirtualMachineDisk(c echo.Context) error {
	var request diskResizeRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if request.DiskMiB <= 0 {
		return badRequest("disk_mib must be positive")
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	if err := virtualMachine.ResizeDisk(c.Request().Context(), request.DiskMiB); err != nil {
		return err
	}

	return s.respondWithVirtualMachine(c, http.StatusAccepted, virtualMachine)
}

func needsMoreThanAvailable(requested, current, available int) bool {
	return requested-current > available
}
