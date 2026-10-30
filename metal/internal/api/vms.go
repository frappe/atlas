package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Create a virtual machine
// @Description	Store the desired state for a virtual machine. A retry with the same first request is safe.
// @ID			createVirtualMachine
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Param		id		path		string			true	"Virtual machine identifier"
// @Param		request	body		createRequest	true	"Complete desired specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id} [put]
func (s *Server) createVirtualMachine(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	var request createRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	specification := request.specification()
	information, err := s.virtualMachineManager.Create(c.Request().Context(), identifier, specification)
	if err != nil {
		return err
	}

	s.wakeReconciler()
	return c.JSON(http.StatusAccepted, toVirtualMachine(information))
}

// @Summary	List virtual machines
// @Description	Return the desired and observed state for each virtual machine on this host.
// @ID			listVirtualMachines
// @Tags		Virtual machines
// @Produce	json
// @Success	200	{array}		virtualMachineResponse
// @Failure	401	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/vms [get]
func (s *Server) listVirtualMachines(c echo.Context) error {
	ctx := c.Request().Context()
	virtualMachines, _, err := s.virtualMachineManager.List(ctx)
	if err != nil {
		return err
	}

	responses := make([]virtualMachineResponse, 0, len(virtualMachines))
	for _, information := range virtualMachines {
		responses = append(responses, toVirtualMachine(information))
	}

	return c.JSON(http.StatusOK, responses)
}

// @Summary	Get a virtual machine
// @Description	Return the desired and observed state for one virtual machine.
// @ID			getVirtualMachine
// @Tags		Virtual machines
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	400	{object}	errorResponse
// @Failure	401	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/vms/{id} [get]
func (s *Server) getVirtualMachine(c echo.Context) error {
	information, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, information)
}

// loadVirtualMachine reads the VM named in the request path.
func (s *Server) loadVirtualMachine(c echo.Context) (vm.Information, error) {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return vm.Information{}, err
	}
	return s.virtualMachineManager.Information(c.Request().Context(), identifier)
}

// respondWithVirtualMachine writes one VM as the response body.
func (s *Server) respondWithVirtualMachine(c echo.Context, status int, information vm.Information) error {
	return c.JSON(status, toVirtualMachine(information))
}

// virtualMachineID reads and validates the VM identifier in the request path.
func virtualMachineID(c echo.Context) (string, error) {
	identifier := c.Param("id")
	if !validResourceID(identifier) {
		return "", badRequest("invalid virtual machine identifier")
	}

	return identifier, nil
}

// snapshotID reads and validates the snapshot identifier in the request path.
func snapshotID(c echo.Context) (string, error) {
	identifier := c.Param("id")
	if !validResourceID(identifier) {
		return "", badRequest("invalid snapshot identifier")
	}

	return identifier, nil
}
