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

// cpuModes are the first /proc/stat cpu columns in order. guest and guest_nice follow, but Linux
// counts them in user and nice too.
var cpuModes = []string{"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"}

// statScalars maps single-value /proc/stat rows to metrics.
var statScalars = map[string]string{
	"ctxt": "system_context_switches_total", "intr": "system_interrupts_total",
	"processes": "system_forks_total", "procs_running": "system_processes_running",
	"procs_blocked": "system_processes_blocked", "btime": "system_boot_time_seconds",
}

// cpuCollector reads CPU time, frequency, throttling, and scheduler statistics. It sends totals
// for the host or for each package, not for each CPU: no reader needs the history of one CPU,
// and per-CPU series would grow with the CPU count.
type cpuCollector struct {
	procRoot string
	sysRoot  string
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
			cpuSamples, err := cpuSeconds(fields[1:], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, cpuSamples...)
		case strings.HasPrefix(fields[0], "cpu"):
			cpuCount++
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

// cpuSeconds reports the time of all CPUs in each mode.
func cpuSeconds(fields []string, path string) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for index, mode := range cpuModes[:min(len(cpuModes), len(fields))] {
		ticks, err := parseNumber(fields[index], path)
		if err != nil {
			return nil, err
		}
		labels := telemetry.Labels{"cpu": "all", "mode": mode}
		samples = append(samples, gauge("system_cpu_seconds_total", ticks/clockTicksPerSecond, labels))
	}
	return samples, nil
}

// frequencies reports, for each CPU package, the average frequency of its CPUs and how often its
// cores and the package were throttled for heat.
func (collector *cpuCollector) frequencies() []telemetry.Sample {
	directories, _ := filepath.Glob(filepath.Join(collector.sysRoot, "devices", "system", "cpu", "cpu[0-9]*"))

	packages := make(map[string]*physicalPackage)
	for _, directory := range directories {
		id := readText(filepath.Join(directory, "topology", "physical_package_id"))
		if packages[id] == nil {
			packages[id] = &physicalPackage{coreThrottles: make(map[string]float64)}
		}
		packages[id].add(directory)
	}

	var samples []telemetry.Sample
	for id, cpuPackage := range packages {
		samples = append(samples, cpuPackage.samples(telemetry.Labels{"package": id})...)
	}
	return samples
}

// physicalPackage adds up the cpufreq and thermal_throttle attributes of the CPUs of one package.
type physicalPackage struct {
	frequencyHertz      float64
	frequencies         int
	coreThrottles       map[string]float64
	packageThrottles    float64
	hasPackageThrottles bool
}

func (cpuPackage *physicalPackage) add(directory string) {
	if kilohertz, err := readNumber(filepath.Join(directory, "cpufreq", "scaling_cur_freq")); err == nil {
		cpuPackage.frequencyHertz += kilohertz * 1000
		cpuPackage.frequencies++
	}
	// The hyperthreads of one core report the count of that core, and each CPU the count of its package.
	if count, err := readNumber(filepath.Join(directory, "thermal_throttle", "core_throttle_count")); err == nil {
		core := readText(filepath.Join(directory, "topology", "core_id"))
		if core == "" {
			core = filepath.Base(directory)
		}
		cpuPackage.coreThrottles[core] = count
	}
	if count, err := readNumber(filepath.Join(directory, "thermal_throttle", "package_throttle_count")); err == nil {
		cpuPackage.packageThrottles, cpuPackage.hasPackageThrottles = count, true
	}
}

func (cpuPackage *physicalPackage) samples(labels telemetry.Labels) []telemetry.Sample {
	var samples []telemetry.Sample
	if cpuPackage.frequencies > 0 {
		average := cpuPackage.frequencyHertz / float64(cpuPackage.frequencies)
		samples = append(samples, gauge("system_cpu_frequency_hertz", average, labels))
	}
	if len(cpuPackage.coreThrottles) > 0 {
		total := 0.0
		for _, count := range cpuPackage.coreThrottles {
			total += count
		}
		samples = append(samples, gauge("system_cpu_core_throttles_total", total, labels))
	}
	if cpuPackage.hasPackageThrottles {
		samples = append(samples, gauge("system_cpu_package_throttles_total", cpuPackage.packageThrottles, labels))
	}
	return samples
}

// schedstat reads the time that tasks ran and waited on the run queues of all CPUs.
func (collector *cpuCollector) schedstat() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "schedstat")
	lines, err := readFields(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var running, waiting, timeslices float64
	for _, fields := range lines {
		// cpuN fields: 7 = running nanoseconds, 8 = waiting nanoseconds, 9 = timeslices.
		if !strings.HasPrefix(fields[0], "cpu") || len(fields) < 10 {
			continue
		}
		cpuRunning, runningError := strconv.ParseFloat(fields[7], 64)
		cpuWaiting, waitingError := strconv.ParseFloat(fields[8], 64)
		cpuTimeslices, timesliceError := strconv.ParseFloat(fields[9], 64)
		if err := errors.Join(runningError, waitingError, timesliceError); err != nil {
			return nil, fmt.Errorf("parse schedstat: %w", err)
		}
		running, waiting, timeslices = running+cpuRunning, waiting+cpuWaiting, timeslices+cpuTimeslices
	}
	return []telemetry.Sample{
		gauge("system_schedstat_running_seconds_total", running/1e9, nil),
		gauge("system_schedstat_waiting_seconds_total", waiting/1e9, nil),
		gauge("system_schedstat_timeslices_total", timeslices, nil),
	}, nil
}
