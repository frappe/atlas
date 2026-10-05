package api

import (
	"net/http"
	"strconv"
	"time"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/metrics"
)

const (
	defaultMetricsRange      = 24 * time.Hour
	defaultMetricsMaxSamples = 300
	minimumMetricsMaxSamples = 10
	maximumMetricsMaxSamples = 2000
)

type virtualMachineMetricsResponse struct {
	Samples               []virtualMachineMetricsSample `json:"samples"`
	SampleIntervalSeconds int                           `json:"sample_interval_seconds,omitempty"`
}

type virtualMachineMetricsSample struct {
	Timestamp int64                `json:"timestamp"`
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
	SizeMiB              int    `json:"size_mib"`
	UsedMiB              int    `json:"used_mib"`
	ThroughputLimitMiBps int    `json:"throughput_limit_mibps"`
	IOPSLimit            int    `json:"iops_limit"`
	ReadBytesPerSecond   uint64 `json:"read_bytes_per_second"`
	WriteBytesPerSecond  uint64 `json:"write_bytes_per_second"`
	ReadMilliIOPS        uint64 `json:"read_milli_iops"`
	WriteMilliIOPS       uint64 `json:"write_milli_iops"`
}

type networkUsageResponse struct {
	ReceivedBytes     uint64 `json:"received_bytes"`
	ReceivedPackets   uint64 `json:"received_packets"`
	SentBytes         uint64 `json:"sent_bytes"`
	SentPackets       uint64 `json:"sent_packets"`
	SentICMPPackets   uint64 `json:"sent_icmp_packets"`
	SentUDPPackets    uint64 `json:"sent_udp_packets"`
	SentTCPSYNPackets uint64 `json:"sent_tcp_syn_packets"`
	SentTCPRSTPackets uint64 `json:"sent_tcp_rst_packets"`
}

// @Summary Read virtual machine metrics history
// @ID getVirtualMachineMetrics
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param start query string false "Inclusive RFC 3339 timestamp"
// @Param end query string false "Exclusive RFC 3339 timestamp"
// @Param max_samples query int false "Maximum samples to return, 10 to 2000" default(300)
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
	start, end := now.Add(-defaultMetricsRange), now
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
	maxSamples := defaultMetricsMaxSamples
	if value := c.QueryParam("max_samples"); value != "" {
		maxSamples, err = strconv.Atoi(value)
		if err != nil || maxSamples < minimumMetricsMaxSamples || maxSamples > maximumMetricsMaxSamples {
			return newAPIError(http.StatusBadRequest, "invalid_request", "max_samples must be an integer from 10 to 2000")
		}
	}
	if _, err := s.virtualMachineManager.Information(c.Request().Context(), identifier); err != nil {
		return err
	}

	interval := metrics.Step(end.Sub(start), maxSamples)
	records, err := s.metricsStore.DownsampledHistory(c.Request().Context(), identifier, start, end, interval)
	if err != nil {
		return err
	}
	response := virtualMachineMetricsResponse{Samples: make([]virtualMachineMetricsSample, 0, len(records)), SampleIntervalSeconds: int(interval / time.Second)}
	for _, record := range records {
		sample := toVirtualMachineMetrics(record.Metrics)
		sample.Timestamp = record.Timestamp.Unix()
		response.Samples = append(response.Samples, sample)
	}
	return c.JSON(http.StatusOK, response)
}

func toVirtualMachineMetrics(sample metrics.Sample) virtualMachineMetricsSample {
	return virtualMachineMetricsSample{
		Up: sample.Up,
		Compute: computeUsageResponse{
			CPUMicroseconds: sample.CPUTimeMicroseconds,
			MemoryBytes:     sample.MemoryBytes,
		},
		Disk: diskUsageResponse{
			SizeMiB:              sample.DiskMiB,
			UsedMiB:              sample.DiskUsedMiB,
			ThroughputLimitMiBps: sample.DiskThroughputLimitMiBps,
			IOPSLimit:            sample.DiskIOPSLimit,
			ReadBytesPerSecond:   sample.DiskReadBytesPerSecond,
			WriteBytesPerSecond:  sample.DiskWriteBytesPerSecond,
			ReadMilliIOPS:        sample.DiskReadMilliIOPS,
			WriteMilliIOPS:       sample.DiskWriteMilliIOPS,
		},
		Network: networkUsageResponse{
			ReceivedBytes:     sample.ReceivedBytes,
			ReceivedPackets:   sample.ReceivedPackets,
			SentBytes:         sample.SentBytes,
			SentPackets:       sample.SentPackets,
			SentICMPPackets:   sample.SentICMPPackets,
			SentUDPPackets:    sample.SentUDPPackets,
			SentTCPSYNPackets: sample.SentTCPSYNPackets,
			SentTCPRSTPackets: sample.SentTCPRSTPackets,
		},
	}
}
