package api

import (
	"net/http"
	"time"

	"github.com/labstack/echo/v5"

	"github.com/frappe/atlas/metal/internal/vm"
)

// cpuBurstStatusResponse is a burstable VM's sliding window average and current CPU limit.
type cpuBurstStatusResponse struct {
	CPUMillicores         int `json:"cpu_millicores"`
	BaselineCPUMillicores int `json:"baseline_cpu_millicores"`
	// ReleaseCPUMillicores is the average at or below which a limited VM gets its full CPU again.
	ReleaseCPUMillicores int `json:"release_cpu_millicores"`
	SlidingWindowMinutes int `json:"sliding_window_minutes"`
	// AverageCPUMillicores is omitted until metald has CPU samples of the VM in memory.
	AverageCPUMillicores *uint64 `json:"average_cpu_millicores,omitempty"`
	// CPULimitMillicores is omitted when the VM is not running.
	CPULimitMillicores *int `json:"cpu_limit_millicores,omitempty"`
	IsLimited          bool `json:"is_limited"`
	// UpSeconds is the time of the sample pairs in the average.
	UpSeconds int64 `json:"up_seconds"`
	// IdleSeconds is the missing history that the average counts as idle.
	IdleSeconds int64      `json:"idle_seconds"`
	ResetAt     *time.Time `json:"reset_at,omitempty"`
}

func newCPUBurstStatusResponse(status vm.CPUBurstStatus) cpuBurstStatusResponse {
	response := cpuBurstStatusResponse{
		CPUMillicores:         status.CPUMillicores,
		BaselineCPUMillicores: status.BaselineCPUMillicores,
		ReleaseCPUMillicores:  vm.ReleaseCPUMillicores(status.BaselineCPUMillicores),
		SlidingWindowMinutes:  status.SlidingWindowMinutes,
		CPULimitMillicores:    status.CPULimit,
		IsLimited:             status.CPULimit != nil && *status.CPULimit < status.CPUMillicores,
	}
	if status.Usage != nil {
		response.AverageCPUMillicores = &status.Usage.MilliCPUs
		response.UpSeconds = int64(status.Usage.UpTime.Seconds())
		response.IdleSeconds = int64(status.Usage.IdleTime.Seconds())
	}
	if !status.ResetAt.IsZero() {
		response.ResetAt = &status.ResetAt
	}
	return response
}

// @Summary Read virtual machine CPU burst status
// @Description Read the sliding window average CPU use and the current CPU limit of a burstable VM.
// @ID getVirtualMachineCPUBurst
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Success 200 {object} cpuBurstStatusResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/cpu-burst [get]
func (s *Server) getVirtualMachineCPUBurst(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	status, err := s.virtualMachineManager.CPUBurstStatus(c.Request().Context(), identifier)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, newCPUBurstStatusResponse(status))
}

// @Summary Reset virtual machine CPU burst
// @Description Store the current time as the CPU burst reset time. The sliding window average ignores older CPU samples. The current CPU limit stays.
// @ID resetVirtualMachineCPUBurst
// @Tags Virtual machines
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Success 200 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/cpu-burst/reset [post]
func (s *Server) resetVirtualMachineCPUBurst(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	if err := s.virtualMachineManager.ResetCPUBurst(c.Request().Context(), identifier); err != nil {
		return err
	}
	return s.respondWithCurrentVirtualMachine(c, http.StatusOK)
}
