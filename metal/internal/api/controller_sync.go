package api

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/host"
	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// syncRequest carries the complete controller-owned host state. Every set is
// complete: a missing field is rejected rather than read as an empty set.
type syncRequest struct {
	WireGuardPeers        []wireGuardPeerRequest `json:"wireguard_peers"`
	Images                []imageRequest         `json:"images"`
	PrivilegedVMAddresses []string               `json:"privileged_vm_addresses"`
	// Unicast selects the unicast NDP transport. The transport builds its peer set from the WireGuard peers.
	Unicast bool `json:"unicast"`
}

// wireGuardPeerRequest is one desired WireGuard peer.
type wireGuardPeerRequest struct {
	Node              string `json:"node"`
	MeshAddress       string `json:"mesh_address"`
	PublicKey         string `json:"public_key"`
	Address           string `json:"address"`
	PublicAddress     string `json:"public_address"`
	PrivateAddress    string `json:"private_address"`
	PrivateNetworkMAC string `json:"private_network_mac_address"`
}

// syncResponse returns host capacity and virtual machine state in the same
// exchange as the sync.
type syncResponse struct {
	Capacity capacityResponse `json:"capacity"`
	// VirtualMachines maps a VM identifier to its last observed state.
	VirtualMachines map[string]virtualMachineStateResponse `json:"virtual_machines"`
	// PrivateNetworkMAC is the MAC of the mesh uplink. It is empty when the mesh is disabled.
	PrivateNetworkMAC string `json:"private_network_mac_address,omitempty"`
}

// virtualMachineStateResponse is the state of one virtual machine on this host.
type virtualMachineStateResponse struct {
	Status string `json:"status"`
	// Routes are the desired VM routes the host holds.
	Routes []routeResponse `json:"routes"`
}

// capacityResponse is what the controller needs to place the next VM.
type capacityResponse struct {
	TotalCPUMillicores     int `json:"total_cpu_millicores"`
	AvailableCPUMillicores int `json:"available_cpu_millicores"`
	VirtualMachineCount    int `json:"virtual_machine_count"`
	TotalMemoryMiB         int `json:"total_memory_mib"`
	AvailableMemoryMiB     int `json:"available_memory_mib"`
	TotalStorageMiB        int `json:"total_storage_mib"`
	AvailableStorageMiB    int `json:"available_storage_mib"`
}

// @Summary	Exchange controller and host state
// @Description	Replace the controller-owned host sets and return current host capacity.
// @ID			synchronizeHost
// @Tags		Host synchronization
// @Accept		json
// @Produce	json
// @Param		request	body		syncRequest	true	"Complete controller state"
// @Success	200		{object}	syncResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	409		{object}	errorResponse
// @Failure	422		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Failure	503		{object}	errorResponse
// @Router		/v1/sync [post]
func (s *Server) exchangeControllerState(c echo.Context) error {
	var request syncRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if request.WireGuardPeers == nil {
		return badRequest("wireguard_peers is required")
	}
	if request.Images == nil {
		return badRequest("images is required")
	}
	// A missing field would read as an empty set and clear the whitelist.
	if request.PrivilegedVMAddresses == nil {
		return badRequest("privileged_vm_addresses is required")
	}
	for _, image := range request.Images {
		if err := image.validate(); err != nil {
			return badRequest(err.Error())
		}
	}
	if err := request.validateMeshPeers(); err != nil {
		return badRequest(err.Error())
	}

	result, err := s.hostService.Synchronize(c.Request().Context(), host.DesiredState{
		WireGuardPeers: request.wireGuardPeers(), Images: request.imagePolicies(),
		PrivilegedVirtualMachineAddresses: request.PrivilegedVMAddresses,
		UnicastEnabled:                    request.Unicast,
	})
	if err != nil {
		return synchronizationFailure(err)
	}

	return c.JSON(http.StatusOK, syncResponse{
		Capacity:          capacityResponseFromHost(result.Capacity),
		VirtualMachines:   virtualMachineStateResponses(result.VirtualMachines),
		PrivateNetworkMAC: result.PrivateNetworkMAC,
	})
}

// synchronizationFailure keeps a host not-found out of the response. This
// endpoint addresses no resource, so a 404 stops the controller when it must
// try the request again.
func synchronizationFailure(err error) error {
	if errors.Is(err, vm.ErrNotFound) || errors.Is(err, storage.ErrNotFound) {
		unavailable := newAPIError(http.StatusServiceUnavailable, "unavailable", "host state is incomplete")
		return fmt.Errorf("%w: %w", unavailable, err)
	}
	return err
}

// imagePolicies converts the requested images into image policies.
func (request syncRequest) imagePolicies() []vm.Image {
	images := make([]vm.Image, 0, len(request.Images))
	for _, image := range request.Images {
		images = append(images, image.specification())
	}
	return images
}

// validateMeshPeers rejects a peer set that atlas-wg-mesh would refuse. WireGuard
// applies the set before the mesh reads it, so the whole set is checked first.
func (request syncRequest) validateMeshPeers() error {
	seen := make(map[string]struct{}, 2*len(request.WireGuardPeers))
	for _, peer := range request.WireGuardPeers {
		address, err := netip.ParseAddr(peer.PublicAddress)
		if err != nil || !address.Is4() {
			return fmt.Errorf("peer %q has no public IPv4 address", peer.Node)
		}
		if peer.PrivateAddress != "" {
			privateAddress, err := netip.ParseAddr(peer.PrivateAddress)
			if err != nil || !privateAddress.Is4() {
				return fmt.Errorf("peer %q has an invalid private IPv4 address", peer.Node)
			}
		}
		mac, err := net.ParseMAC(peer.PrivateNetworkMAC)
		if err != nil || len(mac) != 6 || mac[0]&1 == 1 {
			return fmt.Errorf("peer %q has no unicast MAC address", peer.Node)
		}
		for _, value := range []string{peer.PublicAddress, mac.String()} {
			if _, found := seen[value]; found {
				return fmt.Errorf("peer %q repeats %s", peer.Node, value)
			}
			seen[value] = struct{}{}
		}
	}
	return nil
}

// wireGuardPeers converts the requested peers into the network form.
func (request syncRequest) wireGuardPeers() []network.WireGuardPeer {
	peers := make([]network.WireGuardPeer, 0, len(request.WireGuardPeers))
	for _, peer := range request.WireGuardPeers {
		peers = append(peers, network.WireGuardPeer{
			Node:              peer.Node,
			MeshAddress:       peer.MeshAddress,
			PublicKey:         peer.PublicKey,
			Address:           peer.Address,
			PublicAddress:     peer.PublicAddress,
			PrivateAddress:    peer.PrivateAddress,
			PrivateNetworkMAC: peer.PrivateNetworkMAC,
		})
	}
	return peers
}

// capacityResponseFromHost converts host capacity into the response form.
func capacityResponseFromHost(capacity host.Capacity) capacityResponse {
	return capacityResponse{
		TotalCPUMillicores:     capacity.TotalCPUMillicores,
		AvailableCPUMillicores: capacity.AvailableCPUMillicores,
		VirtualMachineCount:    capacity.VirtualMachineCount,
		TotalMemoryMiB:         capacity.TotalMemoryMiB,
		AvailableMemoryMiB:     capacity.AvailableMemoryMiB,
		TotalStorageMiB:        capacity.TotalStorageMiB,
		AvailableStorageMiB:    capacity.AvailableStorageMiB,
	}
}

// virtualMachineStateResponses converts host reports into the response form.
func virtualMachineStateResponses(reports map[string]host.VirtualMachineReport) map[string]virtualMachineStateResponse {
	responses := make(map[string]virtualMachineStateResponse, len(reports))
	for identifier, report := range reports {
		responses[identifier] = virtualMachineStateResponse{
			Status: string(report.State),
			Routes: toRoutes(report.Routes),
		}
	}
	return responses
}
