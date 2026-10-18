package telemetry

import (
	"context"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// clockTicksPerSecond is USER_HZ, which Linux fixes at 100 for /proc.
const clockTicksPerSecond = 100

// ProcessCollector reports the metald process: restarts show as a new start time.
type ProcessCollector struct {
	startTime time.Time
	build     Labels
}

// NewProcessCollector reports this process with build labels on metal_build_info.
func NewProcessCollector(build Labels) *ProcessCollector {
	return &ProcessCollector{startTime: time.Now(), build: build}
}

// Collect reads Go runtime and /proc/self values.
func (collector *ProcessCollector) Collect(context.Context) ([]Sample, error) {
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)

	samples := []Sample{
		{Metric: "metal_build_info", Value: 1, Labels: collector.build},
		{Metric: "metal_process_start_time_seconds", Value: float64(collector.startTime.Unix())},
		{Metric: "metal_process_goroutines", Value: float64(runtime.NumGoroutine())},
		{Metric: "metal_process_heap_bytes", Value: float64(memory.HeapAlloc)},
		{Metric: "metal_process_memory_from_system_bytes", Value: float64(memory.Sys)},
		{Metric: "metal_process_gc_cycles_total", Value: float64(memory.NumGC)},
		{Metric: "metal_process_gc_pause_seconds_total", Value: float64(memory.PauseTotalNs) / 1e9},
	}

	descriptors, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		return samples, fmt.Errorf("read open descriptors: %w", err)
	}
	samples = append(samples, Sample{Metric: "metal_process_open_descriptors", Value: float64(len(descriptors))})

	cpuSeconds, residentBytes, err := readProcessStat()
	if err != nil {
		return samples, err
	}
	return append(samples,
		Sample{Metric: "metal_process_cpu_seconds_total", Value: cpuSeconds},
		Sample{Metric: "metal_process_resident_memory_bytes", Value: residentBytes},
	), nil
}

func readProcessStat() (float64, float64, error) {
	content, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0, 0, fmt.Errorf("read process stat: %w", err)
	}

	// The command name can hold spaces, so fields start after its closing parenthesis.
	_, rest, found := strings.Cut(string(content), ") ")
	fields := strings.Fields(rest)
	if !found || len(fields) < 22 {
		return 0, 0, fmt.Errorf("unexpected process stat format")
	}

	// fields[11] and fields[12] are utime and stime; fields[21] is rss in pages.
	userTicks, userError := strconv.ParseFloat(fields[11], 64)
	systemTicks, systemError := strconv.ParseFloat(fields[12], 64)
	residentPages, residentError := strconv.ParseFloat(fields[21], 64)
	if userError != nil || systemError != nil || residentError != nil {
		return 0, 0, fmt.Errorf("parse process stat")
	}
	return (userTicks + systemTicks) / clockTicksPerSecond, residentPages * float64(os.Getpagesize()), nil
}
