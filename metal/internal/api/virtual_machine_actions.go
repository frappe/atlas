package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Start a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	202	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/actions/start [post]
func (s *Server) startVirtualMachine(c echo.Context) error {
	return s.setDesiredState(c, vm.StateRunning)
}

// @Summary	Stop a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	202	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/actions/stop [post]
func (s *Server) stopVirtualMachine(c echo.Context) error {
	return s.setDesiredState(c, vm.StateStopped)
}

// @Summary	Pause a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	202	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/actions/pause [post]
func (s *Server) pauseVirtualMachine(c echo.Context) error {
	return s.setDesiredState(c, vm.StatePaused)
}

// @Summary	Resume a paused virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	202	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/actions/resume [post]
func (s *Server) resumeVirtualMachine(c echo.Context) error {
	return s.setDesiredState(c, vm.StateRunning)
}

// @Summary	Terminate a virtual machine
// @Tags		vms
// @Param		id	path	string	true	"Virtual machine identifier"
// @Success	202	"Accepted"
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id}/actions/terminate [post]
func (s *Server) terminateVirtualMachine(c echo.Context) error {
	if err := s.virtualMachineDriver.SetDesiredState(c.Request().Context(), c.Param("id"), vm.StateDestroyed); err != nil {
		return err
	}

	s.wakeReconciler()
	return c.NoContent(http.StatusAccepted)
}

func (s *Server) setDesiredState(c echo.Context, desiredState vm.State) error {
	if err := s.virtualMachineDriver.SetDesiredState(c.Request().Context(), c.Param("id"), desiredState); err != nil {
		return err
	}

	s.wakeReconciler()
	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusAccepted, virtualMachine)
}
