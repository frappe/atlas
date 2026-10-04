package api

import (
	"context"
	"fmt"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// resizeRequest is the complete resource and idle shutdown shape of a VM.
type resizeRequest struct {
	CPUMillicores         int `json:"cpu_millicores"`
	MemoryMiB             int `json:"memory_mib"`
	DiskMiB               int `json:"disk_mib"`
	SleepAfterIdleSeconds int `json:"sleep_after_idle_seconds"`
}

func (r resizeRequest) validate() error {
	if err := (computeRequest{CPUMillicores: r.CPUMillicores, MemoryMiB: r.MemoryMiB, SleepAfterIdleSeconds: r.SleepAfterIdleSeconds}).validate(); err != nil {
		return err
	}
	if r.DiskMiB <= 0 {
		return fmt.Errorf("disk_mib must be positive")
	}
	return nil
}

// @Summary	Resize a virtual machine
// @Description	Store the complete CPU, memory, disk, and idle shutdown shape. The virtual machine must be stopped.
// @ID			resizeVirtualMachine
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Param		id		path	string			true	"Virtual machine identifier"
// @Param		request	body	resizeRequest	true	"Complete resize specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/resize [put]
func (s *Server) resizeVirtualMachine(c echo.Context) error {
	var request resizeRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	admit := func(ctx context.Context, current vm.Information) error {
		capacity, err := s.hostService.Capacity(ctx)
		if err != nil {
			return err
		}
		if needsMoreThanAvailable(request.MemoryMiB, current.MemoryMiB, capacity.AvailableMemoryMiB) ||
			needsMoreThanAvailable(request.DiskMiB, current.DiskMiB, capacity.AvailableStorageMiB) {
			return newAPIError(http.StatusConflict, insufficientCapacityCode, "not enough host capacity")
		}
		return nil
	}
	if err := s.virtualMachineManager.Resize(c.Request().Context(), identifier, vm.Compute{
		CPUMillicores: request.CPUMillicores, MemoryMiB: request.MemoryMiB,
		SleepAfterIdleSeconds: request.SleepAfterIdleSeconds,
	}, request.DiskMiB, admit); err != nil {
		return err
	}

	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}
