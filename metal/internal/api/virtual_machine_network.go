package api

import (
	"fmt"
	"net/http"
	"net/netip"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

type networkUpdateRequest struct {
	Egress                       string `json:"egress"`
	PublicIPv4                   string `json:"public_ipv4"`
	PrivateNetworkThroughputMbps int    `json:"private_network_throughput_mbps"`
	PublicNetworkThroughputMbps  int    `json:"public_network_throughput_mbps"`
}

// @Summary	Update mutable VM network settings
// @Description	Apply public IPv4, host egress, and throughput settings without restarting the VM.
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path	string				true	"Virtual machine identifier"
// @Param		body	body	networkUpdateRequest	true	"Network settings"
// @Success	200	{object}	virtualMachineResponse
// @Failure	400	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/network [put]
func (s *Server) updateVirtualMachineNetwork(c echo.Context) error {
	var request networkUpdateRequest
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
	if err := s.virtualMachineDriver.UpdateNetwork(c.Request().Context(), virtualMachine.ID(), request.update()); err != nil {
		return err
	}

	updatedVirtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, updatedVirtualMachine)
}

func (request networkUpdateRequest) validate() error {
	egress := vm.Egress(request.Egress)
	if !egress.IsValid() {
		return fmt.Errorf("egress must be %s, %s, or %s", vm.EgressUplink, vm.EgressMesh, vm.EgressNone)
	}
	if request.PrivateNetworkThroughputMbps < 0 || request.PublicNetworkThroughputMbps < 0 {
		return fmt.Errorf("network throughput values must not be negative")
	}
	if request.PublicIPv4 == "" {
		return nil
	}
	publicAddress, err := netip.ParseAddr(request.PublicIPv4)
	if err != nil || !publicAddress.Is4() {
		return fmt.Errorf("public_ipv4 must be an IPv4 address")
	}
	if !egress.HasInternetPath() {
		return fmt.Errorf("public_ipv4 requires %s egress", vm.EgressUplink)
	}
	return nil
}

func (request networkUpdateRequest) update() vm.NetworkUpdate {
	return vm.NetworkUpdate{
		Egress:                       vm.Egress(request.Egress),
		PublicIPv4:                   request.PublicIPv4,
		PrivateNetworkThroughputMbps: request.PrivateNetworkThroughputMbps,
		PublicNetworkThroughputMbps:  request.PublicNetworkThroughputMbps,
	}
}
