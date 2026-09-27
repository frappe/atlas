package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// virtualMachineMetricsResponse describes the current resource use of one
// virtual machine.
type virtualMachineMetricsResponse struct {
	Compute computeUsageResponse `json:"compute"`
	Disk    diskUsageResponse    `json:"disk"`
	Network networkUsageResponse `json:"network"`
}

// computeUsageResponse holds cumulative CPU time and current memory use.
type computeUsageResponse struct {
	CPUMicroseconds uint64 `json:"cpu_microseconds"`
	MemoryBytes     uint64 `json:"memory_bytes"`
}

// diskUsageResponse holds the disk's requested size and its use as of the
// last reconcile pass.
type diskUsageResponse struct {
	SizeMiB int `json:"size_mib"`
	UsedMiB int `json:"used_mib"`
}

// networkUsageResponse holds cumulative received and sent bytes and packets.
type networkUsageResponse struct {
	ReceivedBytes   uint64 `json:"received_bytes"`
	ReceivedPackets uint64 `json:"received_packets"`
	SentBytes       uint64 `json:"sent_bytes"`
	SentPackets     uint64 `json:"sent_packets"`
}

// @Summary	Read virtual machine metrics
// @Description	Return the current CPU, memory, disk, and network use of one virtual machine. CPU and network values are cumulative counters since the guest's current process started. Memory is a point-in-time gauge. Disk values are as fresh as the last reconcile pass. A virtual machine that is neither running nor paused reports its disk alone.
// @ID			getVirtualMachineMetrics
// @Tags		Virtual machines
// @Produce	json
// @Param		id	path		string	true	"Virtual machine identifier"
// @Success	200	{object}	virtualMachineMetricsResponse
// @Failure	401	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/vms/{id}/metrics [get]
func (s *Server) getVirtualMachineMetrics(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	metrics, err := s.virtualMachineManager.Metrics(c.Request().Context(), identifier)
	if err != nil {
		return err
	}

	return c.JSON(http.StatusOK, toVirtualMachineMetrics(metrics))
}

// toVirtualMachineMetrics converts domain metrics to their response shape.
func toVirtualMachineMetrics(metrics vm.Metrics) virtualMachineMetricsResponse {
	return virtualMachineMetricsResponse{
		Compute: computeUsageResponse{
			CPUMicroseconds: metrics.CPUUsageMicroseconds,
			MemoryBytes:     metrics.MemoryBytes,
		},
		Disk: diskUsageResponse{
			SizeMiB: metrics.DiskMiB,
			UsedMiB: metrics.DiskUsedMiB,
		},
		Network: networkUsageResponse{
			ReceivedBytes:   metrics.ReceivedBytes,
			ReceivedPackets: metrics.ReceivedPackets,
			SentBytes:       metrics.SentBytes,
			SentPackets:     metrics.SentPackets,
		},
	}
}
