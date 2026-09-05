package api

import (
	"maps"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// @Summary	Create or confirm a virtual machine reservation
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string			true	"Virtual machine identifier"
// @Param		body	body		createRequest	true	"Virtual machine specification"
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Router		/vms/{id} [put]
func (s *Server) createVirtualMachine(c echo.Context) error {
	virtualMachineID := c.Param("id")
	if !validResourceID(virtualMachineID) {
		return badRequest("invalid virtual machine identifier")
	}

	var request createRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	specification := request.spec()
	if _, err := s.virtualMachineDriver.Create(c.Request().Context(), virtualMachineID, specification); err != nil {
		return err
	}

	s.wakeReconciler()
	return c.JSON(http.StatusAccepted, virtualMachineReservationResponse(virtualMachineID, specification))
}

// @Summary	List virtual machines
// @Tags		vms
// @Produce	json
// @Success	200	{object}	virtualMachineListResponse
// @Router		/vms [get]
func (s *Server) listVirtualMachines(c echo.Context) error {
	ctx := c.Request().Context()
	virtualMachines, err := s.virtualMachineDriver.List(ctx)
	if err != nil {
		return err
	}

	responses := make([]virtualMachineResponse, 0, len(virtualMachines))
	for _, virtualMachine := range virtualMachines {
		information, err := virtualMachine.Info(ctx)
		if err != nil {
			continue
		}
		responses = append(responses, toVirtualMachine(information))
	}

	return c.JSON(http.StatusOK, virtualMachineListResponse{VMs: responses})
}

// @Summary	Get a virtual machine
// @Tags		vms
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineResponse
// @Failure	404	{object}	errorResponse
// @Router		/vms/{id} [get]
func (s *Server) getVirtualMachine(c echo.Context) error {
	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, virtualMachine)
}

func (s *Server) loadVirtualMachine(c echo.Context) (vm.VM, error) {
	return s.virtualMachineDriver.Load(c.Request().Context(), c.Param("id"))
}

func (s *Server) respondWithVirtualMachine(c echo.Context, status int, virtualMachine vm.VM) error {
	information, err := virtualMachine.Info(c.Request().Context())
	if err != nil {
		return err
	}
	return c.JSON(status, toVirtualMachine(information))
}

func virtualMachineReservationResponse(virtualMachineID string, specification vm.Spec) virtualMachineResponse {
	return virtualMachineResponse{
		ID:           virtualMachineID,
		State:        string(vm.StateUnknown),
		DesiredState: string(vm.StateRunning),
		VCPUs:        specification.VCPUs,
		MemoryMiB:    specification.MemoryMiB,
		Image:        toVirtualMachineImage(specification.Image),
		SSHKeys:      append([]string(nil), specification.SSHKeys...),
		Hostname:     specification.Hostname,
		Metadata:     maps.Clone(specification.Metadata),
		Network: networkResponse{
			PublicIPv4:                    specification.Network.PublicIPv4,
			WireGuardMeshIPv6:             specification.Network.WireGuardMeshIPv6,
			PrivateNetworkThroughputMiBps: specification.Network.PrivateNetworkThroughputMiBps,
			PublicNetworkThroughputMiBps:  specification.Network.PublicNetworkThroughputMiBps,
			Egress:                        string(specification.Network.Egress),
		},
		Disk: diskResponse{
			ThroughputMiBps: specification.Disk.ThroughputMiBps,
			IOPS:            specification.Disk.IOPS,
			SizeMiB:         specification.DiskMiB,
		},
	}
}
