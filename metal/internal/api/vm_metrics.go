package api

import (
	"net/http"
	"time"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/metrics"
	"github.com/frappe/atlas/metal/internal/vm"
)

type virtualMachineMetricsResponse struct {
	Samples []virtualMachineMetricsSample `json:"samples"`
}

type virtualMachineMetricsSample struct {
	Timestamp time.Time            `json:"timestamp"`
	Up        bool                 `json:"up"`
	Compute   computeUsageResponse `json:"compute"`
	Disk      diskUsageResponse    `json:"disk"`
	Network   networkUsageResponse `json:"network"`
}

type computeUsageResponse struct {
	CPUMicroseconds uint64 `json:"cpu_microseconds"`
	MemoryBytes     uint64 `json:"memory_bytes"`
}

type diskUsageResponse struct {
	SizeMiB int `json:"size_mib"`
	UsedMiB int `json:"used_mib"`
}

type networkUsageResponse struct {
	ReceivedBytes   uint64 `json:"received_bytes"`
	ReceivedPackets uint64 `json:"received_packets"`
	SentBytes       uint64 `json:"sent_bytes"`
	SentPackets     uint64 `json:"sent_packets"`
}

// @Summary Read virtual machine metrics history
// @ID getVirtualMachineMetrics
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param start query string false "Inclusive RFC 3339 timestamp"
// @Param end query string false "Exclusive RFC 3339 timestamp"
// @Success 200 {object} virtualMachineMetricsResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/metrics [get]
func (s *Server) getVirtualMachineMetrics(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	now := time.Now().UTC()
	start, end := now.Add(-metrics.Retention), now
	for name, destination := range map[string]*time.Time{"start": &start, "end": &end} {
		if value := c.QueryParam(name); value != "" {
			parsed, err := time.Parse(time.RFC3339Nano, value)
			if err != nil {
				return newAPIError(http.StatusBadRequest, "invalid_request", name+" must be an RFC 3339 timestamp")
			}
			*destination = parsed
		}
	}
	if !start.Before(end) {
		return newAPIError(http.StatusBadRequest, "invalid_request", "start must precede end")
	}
	if start.Before(now.Add(-metrics.Retention)) {
		start = now.Add(-metrics.Retention)
	}
	if end.After(now) {
		end = now
	}
	if _, err := s.virtualMachineManager.Information(c.Request().Context(), identifier); err != nil {
		return err
	}
	records, err := s.metricsStore.History(c.Request().Context(), "vm:"+identifier, start, end)
	if err != nil {
		return err
	}
	response := virtualMachineMetricsResponse{Samples: make([]virtualMachineMetricsSample, 0, len(records))}
	for _, record := range records {
		if record.VM == nil {
			continue
		}
		sample := toVirtualMachineMetrics(*record.VM)
		sample.Timestamp = record.Timestamp
		response.Samples = append(response.Samples, sample)
	}
	return c.JSON(http.StatusOK, response)
}

func toVirtualMachineMetrics(metrics vm.Metrics) virtualMachineMetricsSample {
	return virtualMachineMetricsSample{
		Up: metrics.Up,
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
