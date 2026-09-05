package api

import (
	"bufio"
	"context"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/vm"
)

type syncRequest struct {
	WireGuardPeers        []wireGuardPeerRequest `json:"wireguard_peers"`
	Images                []imageRequest         `json:"images"`
	PrivilegedVMAddresses []string               `json:"privileged_vm_addresses"`
}

type wireGuardPeerRequest struct {
	Node      string `json:"node"`
	NodeID    uint32 `json:"node_id"`
	PublicKey string `json:"public_key"`
	Address   string `json:"address"`
}

type syncResponse struct {
	Capacity capacityResponse `json:"capacity"`
}

type capacityResponse struct {
	TotalCPUCount       int `json:"total_cpu_count"`
	AvailableCPUCount   int `json:"available_cpu_count"`
	VirtualMachineCount int `json:"virtual_machine_count"`
	TotalMemoryMiB      int `json:"total_memory_mib"`
	AvailableMemoryMiB  int `json:"available_memory_mib"`
	TotalStorageMiB     int `json:"total_storage_mib"`
	AvailableStorageMiB int `json:"available_storage_mib"`
}

// @Summary	Exchange controller and host state
// @Tags		sync
// @Accept		json
// @Produce	json
// @Param		body	body		syncRequest	true	"Controller state"
// @Success	200		{object}	syncResponse
// @Failure	400		{object}	errorResponse
// @Router		/sync [post]
func (s *Server) exchangeControllerState(c echo.Context) error {
	var request syncRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
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

	ctx := c.Request().Context()
	if err := s.mesh.ApplyPrivilegedAddresses(ctx, request.PrivilegedVMAddresses); err != nil {
		return err
	}
	if err := s.wireGuardManager.Apply(ctx, request.toWireGuardPeers()); err != nil {
		return err
	}
	if err := s.imagePolicyStore.SetImagePolicies(ctx, request.imagePolicies()); err != nil {
		return err
	}
	s.wakeReconciler()

	capacity, err := s.getHostCapacity(ctx)
	if err != nil {
		return err
	}

	return c.JSON(http.StatusOK, syncResponse{Capacity: capacity})
}

func (request syncRequest) imagePolicies() []vm.ImageRef {
	images := make([]vm.ImageRef, 0, len(request.Images))
	for _, image := range request.Images {
		images = append(images, image.specification())
	}
	return images
}

func (request syncRequest) toWireGuardPeers() []network.WireGuardPeer {
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

func (s *Server) getHostCapacity(ctx context.Context) (capacityResponse, error) {
	availableCPUCount, availableMemory, virtualMachineCount, err := s.getComputeCapacity(ctx)
	if err != nil {
		return capacityResponse{}, err
	}

	var information syscall.Sysinfo_t
	if err := syscall.Sysinfo(&information); err != nil {
		return capacityResponse{}, err
	}
	unit := uint64(information.Unit)
	if unit == 0 {
		unit = 1
	}

	storageCapacity, err := s.storage.Capacity(ctx)
	if err != nil {
		return capacityResponse{}, err
	}

	return capacityResponse{
		TotalCPUCount:       runtime.NumCPU(),
		AvailableCPUCount:   availableCPUCount,
		VirtualMachineCount: virtualMachineCount,
		TotalMemoryMiB:      int((uint64(information.Totalram) * unit) >> 20),
		AvailableMemoryMiB:  availableMemory,
		TotalStorageMiB:     int(storageCapacity.TotalMiB),
		AvailableStorageMiB: int(storageCapacity.AvailableMiB),
	}, nil
}

func (s *Server) getComputeCapacity(ctx context.Context) (availableCPUCount, availableMemoryMiB, virtualMachineCount int, err error) {
	virtualMachines, err := s.virtualMachineDriver.List(ctx)
	if err != nil {
		return 0, 0, 0, err
	}

	allocatedCPUCount := 0
	for _, machine := range virtualMachines {
		information, err := machine.Info(ctx)
		if err != nil {
			return 0, 0, 0, err
		}
		allocatedCPUCount += information.VCPUs
	}

	memoryMiB, err := readAvailableMemoryMiB()
	if err != nil {
		return 0, 0, 0, err
	}

	return max(runtime.NumCPU()-allocatedCPUCount, 0), memoryMiB, len(virtualMachines), nil
}

func readAvailableMemoryMiB() (int, error) {
	file, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) != 3 || fields[0] != "MemAvailable:" || fields[2] != "kB" {
			continue
		}

		value, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, err
		}
		return int(value / 1024), nil
	}
	if err := scanner.Err(); err != nil {
		return 0, err
	}

	return 0, fmt.Errorf("MemAvailable is missing from /proc/meminfo")
}
