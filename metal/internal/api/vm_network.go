package api

import (
	"net/http"

	"github.com/labstack/echo/v5"
)

// @Summary	Set the virtual machine network
// @Description	Store the complete network specification. Reconciliation continues after the response.
// @ID			setVirtualMachineNetwork
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Param		id		path	string			true	"Virtual machine identifier"
// @Param		request	body	networkRequest	true	"Complete network specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/network [put]
func (s *Server) setVirtualMachineNetwork(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	var request networkRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineManager.SetNetwork(c.Request().Context(), identifier, request.specification()); err != nil {
		return err
	}

	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}
