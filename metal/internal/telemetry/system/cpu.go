package system

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

const clockTicksPerSecond = 100

// cpuModes are the /proc/stat cpu columns in order.
var cpuModes = []string{"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"}

// statScalars maps single-value /proc/stat rows to metrics.
var statScalars = map[string]string{
	"ctxt": "system_context_switches_total", "intr": "system_interrupts_total",
	"processes": "system_forks_total", "procs_running": "system_processes_running",
	"procs_blocked": "system_processes_blocked", "btime": "system_boot_time_seconds",
}

// cpuCollector reads CPU time per core, frequency, throttling, and scheduler
// statistics. It keeps the previous total CPU time to report the percentages
// that Pilot hosts also send.
type cpuCollector struct {
	procRoot string
	sysRoot  string
	previous []float64
}

// Collect reads /proc/stat, /proc/schedstat, and the cpufreq and thermal
// throttle attributes of each CPU.
func (collector *cpuCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	samples, err := collector.stat()
	if err != nil {
		return nil, err
	}
	samples = append(samples, collector.frequencies()...)

	schedule, err := collector.schedstat()
	return append(samples, schedule...), err
}

func (collector *cpuCollector) stat() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "stat")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	cpuCount := 0
	for _, fields := range lines {
		switch {
		case fields[0] == "cpu":
			percentages, err := collector.percentages(fields[1:], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, percentages...)
		case strings.HasPrefix(fields[0], "cpu"):
			cpuCount++
			cpuSamples, err := cpuSeconds(strings.TrimPrefix(fields[0], "cpu"), fields[1:], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, cpuSamples...)
		case statScalars[fields[0]] != "" && len(fields) > 1:
			value, err := parseNumber(fields[1], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, gauge(statScalars[fields[0]], value, nil))
		}
	}
	return append(samples, gauge("system_cpu_count", float64(cpuCount), nil)), nil
}

// cpuSeconds reports the time of one core in each mode.
func cpuSeconds(cpu string, fields []string, path string) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for index, mode := range cpuModes[:min(len(cpuModes), len(fields))] {
		ticks, err := parseNumber(fields[index], path)
		if err != nil {
			return nil, err
		}
		labels := telemetry.Labels{"cpu": cpu, "mode": mode}
		samples = append(samples, gauge("system_cpu_seconds_total", ticks/clockTicksPerSecond, labels))
	}
	return samples, nil
}

// percentages reports host CPU use since the previous snapshot. It matches the
// Pilot shipper: usage is 100 minus idle, and user includes nice. Guest time is
// already part of user time, so only the first eight modes count.
func (collector *cpuCollector) percentages(fields []string, path string) ([]telemetry.Sample, error) {
	if len(fields) < 8 {
		return nil, fmt.Errorf("parse %s: short cpu line", filepath.Base(path))
	}
	ticks := make([]float64, 8)
	for index := range ticks {
		value, err := parseNumber(fields[index], path)
		if err != nil {
			return nil, err
		}
		ticks[index] = value
	}

	previous := collector.previous
	collector.previous = ticks
	if previous == nil {
		return nil, nil
	}

	delta := make(map[string]float64, len(ticks))
	total := 0.0
	for index, mode := range cpuModes[:8] {
		delta[mode] = ticks[index] - previous[index]
		total += delta[mode]
	}
	if total <= 0 {
		return nil, nil
	}

	percent := func(value float64) float64 { return value / total * 100 }
	samples := []telemetry.Sample{gauge("system_cpu_usage_percent", 100-percent(delta["idle"]), nil)}
	modes := map[string]float64{
		"user": delta["user"] + delta["nice"], "system": delta["system"], "iowait": delta["iowait"],
		"irq": delta["irq"], "softirq": delta["softirq"], "steal": delta["steal"], "idle": delta["idle"],
	}
	for mode, value := range modes {
		samples = append(samples, gauge("system_cpu_mode_percent", percent(value), telemetry.Labels{"mode": mode}))
	}
	return samples, nil
}

// frequencies reads the current, minimum, and maximum frequency of each core
// and how often a core or package was throttled for heat.
func (collector *cpuCollector) frequencies() []telemetry.Sample {
	directories, _ := filepath.Glob(filepath.Join(collector.sysRoot, "devices", "system", "cpu", "cpu[0-9]*"))

	var samples []telemetry.Sample
	packages := make(map[string]bool)
	for _, directory := range directories {
		cpu := strings.TrimPrefix(filepath.Base(directory), "cpu")
		labels := telemetry.Labels{"cpu": cpu}
		for file, metric := range map[string]string{
			"cpufreq/scaling_cur_freq": "system_cpu_frequency_hertz",
			"cpufreq/cpuinfo_min_freq": "system_cpu_frequency_min_hertz",
			"cpufreq/cpuinfo_max_freq": "system_cpu_frequency_max_hertz",
		} {
			if kilohertz, err := readNumber(filepath.Join(directory, file)); err == nil {
				samples = append(samples, gauge(metric, kilohertz*1000, labels))
			}
		}

		if count, err := readNumber(filepath.Join(directory, "thermal_throttle", "core_throttle_count")); err == nil {
			samples = append(samples, gauge("system_cpu_core_throttles_total", count, labels))
		}
		physicalPackage := readText(filepath.Join(directory, "topology", "physical_package_id"))
		if count, err := readNumber(filepath.Join(directory, "thermal_throttle", "package_throttle_count")); err == nil && !packages[physicalPackage] {
			packages[physicalPackage] = true
			samples = append(samples, gauge("system_cpu_package_throttles_total", count, telemetry.Labels{"package": physicalPackage}))
		}
	}
	return samples
}

// schedstat reads time running and waiting on each CPU's run queue.
func (collector *cpuCollector) schedstat() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "schedstat")
	lines, err := readFields(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines {
		// cpuN fields: 7 = running nanoseconds, 8 = waiting nanoseconds, 9 = timeslices.
		if !strings.HasPrefix(fields[0], "cpu") || len(fields) < 10 {
			continue
		}
		labels := telemetry.Labels{"cpu": strings.TrimPrefix(fields[0], "cpu")}
		running, runningError := strconv.ParseFloat(fields[7], 64)
		waiting, waitingError := strconv.ParseFloat(fields[8], 64)
		timeslices, timesliceError := strconv.ParseFloat(fields[9], 64)
		if err := errors.Join(runningError, waitingError, timesliceError); err != nil {
			return nil, fmt.Errorf("parse schedstat: %w", err)
		}
		samples = append(samples,
			gauge("system_schedstat_running_seconds_total", running/1e9, labels),
			gauge("system_schedstat_waiting_seconds_total", waiting/1e9, labels),
			gauge("system_schedstat_timeslices_total", timeslices, labels),
		)
	}
	return samples, nil
}
