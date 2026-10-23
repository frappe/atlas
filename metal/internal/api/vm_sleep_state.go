package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary Upload virtual machine sleep snapshots
// @Description Upload selected artifacts of an already sleeping VM and evict their local copies. Read observed.sleep_snapshot, observed.sleep_state, and observed.error for completion.
// @ID offloadVirtualMachineSleepState
// @Tags Virtual machines
// @Accept json
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param request body vm.SleepStateRequest true "Presigned PUT and GET URLs for memory, disk, or both"
// @Success 202 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/sleep-state [post]
func (s *Server) offloadVirtualMachineSleepState(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	var request vm.SleepStateRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.Validate(false); err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineManager.OffloadSleepState(c.Request().Context(), identifier, request); err != nil {
		return err
	}
	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}

// @Summary Refresh virtual machine sleep download URLs
// @Description Store replacement GET URLs for the named sleep snapshot. Omitted artifacts keep their URLs. This request does not download artifacts.
// @ID refreshVirtualMachineSleepState
// @Tags Virtual machines
// @Accept json
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param request body vm.SleepStateRequest true "Snapshot identifier and refreshed GET URLs; upload_url is not allowed"
// @Success 202 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/sleep-state [patch]
func (s *Server) refreshVirtualMachineSleepState(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	var request vm.SleepStateRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.Validate(true); err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineManager.RefreshSleepState(c.Request().Context(), identifier, request); err != nil {
		return err
	}
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}
