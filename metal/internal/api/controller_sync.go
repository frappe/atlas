package api

import (
	"errors"
	"fmt"
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
	// UnicastPeers is absent in multicast mode and complete in unicast mode.
	UnicastPeers *[]string `json:"unicast_peers"`
}

// wireGuardPeerRequest is one desired WireGuard peer.
type wireGuardPeerRequest struct {
	Node      string `json:"node"`
	NodeID    uint32 `json:"node_id"`
	PublicKey string `json:"public_key"`
	Address   string `json:"address"`
}

// syncResponse returns host capacity and virtual machine state in the same
// exchange as the sync.
type syncResponse struct {
	Capacity capacityResponse `json:"capacity"`
	// VirtualMachines maps a VM identifier to its last observed state.
	VirtualMachines map[string]virtualMachineStateResponse `json:"virtual_machines"`
}

// virtualMachineStateResponse is the state of one virtual machine on this host.
type virtualMachineStateResponse struct {
	Status string `json:"status"`
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
// @Security	BearerAuth
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
	unicastPeers, err := request.unicastPeers()
	if err != nil {
		return badRequest(err.Error())
	}

	result, err := s.hostService.Synchronize(c.Request().Context(), host.DesiredState{
		WireGuardPeers: request.wireGuardPeers(), Images: request.imagePolicies(),
		PrivilegedVirtualMachineAddresses: request.PrivilegedVMAddresses,
		UnicastPeers:                      unicastPeers,
	})
	if err != nil {
		return synchronizationFailure(err)
	}

	return c.JSON(http.StatusOK, syncResponse{
		Capacity:        capacityResponseFromHost(result.Capacity),
		VirtualMachines: virtualMachineStateResponses(result.VirtualMachineStates),
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

// unicastPeers converts the requested peer addresses. An absent field means
// multicast mode and returns nil. A present field must hold strict IPv4
// addresses without duplicates.
func (request syncRequest) unicastPeers() ([]netip.Addr, error) {
	if request.UnicastPeers == nil {
		return nil, nil
	}

	peers := make([]netip.Addr, 0, len(*request.UnicastPeers))
	seen := make(map[netip.Addr]struct{}, len(*request.UnicastPeers))
	for _, value := range *request.UnicastPeers {
		peer, parseErr := netip.ParseAddr(value)
		if parseErr != nil || !peer.Is4() {
			return nil, fmt.Errorf("unicast peer %q is not an IPv4 address", value)
		}
		if _, found := seen[peer]; found {
			return nil, fmt.Errorf("unicast peer %s is repeated", peer)
		}
		seen[peer] = struct{}{}
		peers = append(peers, peer)
	}
	return peers, nil
}

// wireGuardPeers converts the requested peers into the network form.
func (request syncRequest) wireGuardPeers() []network.WireGuardPeer {
	peers := make([]network.WireGuardPeer, 0, len(request.WireGuardPeers))
	for _, peer := range request.WireGuardPeers {
		peers = append(peers, network.WireGuardPeer{
			Node:      peer.Node,
			NodeID:    peer.NodeID,
			PublicKey: peer.PublicKey,
			Address:   peer.Address,
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

// virtualMachineStateResponses converts host states into the response form.
func virtualMachineStateResponses(states map[string]vm.State) map[string]virtualMachineStateResponse {
	responses := make(map[string]virtualMachineStateResponse, len(states))
	for identifier, state := range states {
		responses[identifier] = virtualMachineStateResponse{Status: string(state)}
	}
	return responses
}
