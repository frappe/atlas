package system

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// Kernel adjtimex status bits.
const (
	clockUnsynchronized = 0x0040
	clockNanoseconds    = 0x2000
)

// kernelCollector reads load, pressure, softirqs, interrupts, entropy, and
// open file use.
type kernelCollector struct {
	procRoot string
}

// Collect runs each kernel reader and keeps the others when one fails.
func (collector kernelCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	var failures []error
	for _, read := range []func() ([]telemetry.Sample, error){
		collector.load, collector.pressure, collector.softirqs, collector.interrupts, collector.limits,
	} {
		collected, err := read()
		samples = append(samples, collected...)
		failures = append(failures, err)
	}
	return samples, errors.Join(failures...)
}

func (collector kernelCollector) load() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "loadavg")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}
	if len(lines) == 0 || len(lines[0]) < 4 {
		return nil, fmt.Errorf("parse loadavg: unexpected format")
	}

	var samples []telemetry.Sample
	for index, window := range []string{"1m", "5m", "15m"} {
		value, err := parseNumber(lines[0][index], path)
		if err != nil {
			return nil, err
		}
		samples = append(samples, gauge("system_load_average", value, telemetry.Labels{"window": window}))
	}
	return samples, nil
}

// pressure reads pressure stall totals. A kernel without PSI has no files.
func (collector kernelCollector) pressure() ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for _, resource := range []string{"cpu", "memory", "io", "irq"} {
		path := filepath.Join(collector.procRoot, "pressure", resource)
		lines, err := readFields(path)
		if errors.Is(err, fs.ErrNotExist) || errors.Is(err, unix.EOPNOTSUPP) {
			continue
		}
		if err != nil {
			return samples, err
		}

		for _, fields := range lines {
			for _, field := range fields[1:] {
				microseconds, found := strings.CutPrefix(field, "total=")
				if !found {
					continue
				}
				value, err := parseNumber(microseconds, path)
				if err != nil {
					return samples, err
				}
				labels := telemetry.Labels{"resource": resource, "kind": fields[0]}
				samples = append(samples, gauge("system_pressure_stall_seconds_total", value/1e6, labels))
			}
		}
	}
	return samples, nil
}

// softirqs sums each soft interrupt type over all CPUs.
func (collector kernelCollector) softirqs() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "softirqs")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines[1:] {
		total, err := sumNumbers(fields[1:], path)
		if err != nil {
			return nil, err
		}
		samples = append(samples, gauge("system_softirqs_total", total, telemetry.Labels{"type": strings.TrimSuffix(fields[0], ":")}))
	}
	return samples, nil
}

// interrupts sums each interrupt line over all CPUs and names its device. A
// NIC queue that stops getting interrupts shows here.
func (collector kernelCollector) interrupts() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "interrupts")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}
	if len(lines) == 0 {
		return nil, nil
	}
	cpus := len(lines[0])

	var samples []telemetry.Sample
	for _, fields := range lines[1:] {
		counts := fields[1:min(len(fields), cpus+1)]
		total, err := sumNumbers(counts, path)
		if err != nil {
			continue
		}
		labels := telemetry.Labels{"interrupt": strings.TrimSuffix(fields[0], ":")}
		if rest := fields[1+len(counts):]; len(rest) > 0 {
			labels["type"] = rest[0]
			labels["devices"] = strings.Join(rest[1:], " ")
		}
		samples = append(samples, gauge("system_interrupts_by_source_total", total, labels))
	}
	return samples, nil
}

// limits reads entropy, open files, and the process and thread limits.
func (collector kernelCollector) limits() ([]telemetry.Sample, error) {
	files := map[string]string{
		"sys/kernel/random/entropy_avail": "system_entropy_available_bits",
		"sys/kernel/random/poolsize":      "system_entropy_pool_size_bits",
		"sys/kernel/pid_max":              "system_processes_max_processes",
		"sys/kernel/threads-max":          "system_processes_max_threads",
	}
	var samples []telemetry.Sample
	for file, metric := range files {
		if value, err := readNumber(filepath.Join(collector.procRoot, file)); err == nil {
			samples = append(samples, gauge(metric, value, nil))
		}
	}

	path := filepath.Join(collector.procRoot, "sys", "fs", "file-nr")
	content, err := os.ReadFile(path)
	if err != nil {
		return samples, err
	}
	fields := strings.Fields(string(content))
	if len(fields) < 3 {
		return samples, fmt.Errorf("parse file-nr: unexpected format")
	}
	allocated, allocatedError := parseNumber(fields[0], path)
	limit, limitError := parseNumber(fields[2], path)
	if err := errors.Join(allocatedError, limitError); err != nil {
		return samples, err
	}
	return append(samples,
		gauge("system_open_files", allocated, nil),
		gauge("system_open_files_limit", limit, nil),
	), nil
}

// processCollector counts processes and threads by state.
type processCollector struct {
	procRoot string
}

// Collect reads the state of each process and thread from /proc.
func (collector processCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	processes, err := filepath.Glob(filepath.Join(collector.procRoot, "[0-9]*"))
	if err != nil {
		return nil, err
	}

	states := make(map[string]float64)
	threads := 0.0
	for _, process := range processes {
		tasks, _ := filepath.Glob(filepath.Join(process, "task", "[0-9]*"))
		threads += float64(len(tasks))
		content, err := os.ReadFile(filepath.Join(process, "stat"))
		if err != nil {
			continue
		}
		// The command name can hold spaces, so the state follows its closing parenthesis.
		_, rest, found := strings.Cut(string(content), ") ")
		if found && rest != "" {
			states[rest[:1]]++
		}
	}

	samples := []telemetry.Sample{
		gauge("system_processes_pids", float64(len(processes)), nil),
		gauge("system_processes_threads", threads, nil),
	}
	for state, count := range states {
		samples = append(samples, gauge("system_processes_state", count, telemetry.Labels{"state": state}))
	}
	return samples, nil
}

// timeCollector reads the clock, its discipline, and the kernel and OS identity.
type timeCollector struct{}

// Collect reads adjtimex and uname.
func (timeCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	var timex unix.Timex
	if _, err := unix.Adjtimex(&timex); err != nil {
		return nil, err
	}

	synchronized := 1.0
	if timex.Status&clockUnsynchronized != 0 {
		synchronized = 0
	}
	offsetScale := 1e-6
	if timex.Status&clockNanoseconds != 0 {
		offsetScale = 1e-9
	}

	var name unix.Utsname
	if err := unix.Uname(&name); err != nil {
		return nil, err
	}
	var now unix.Timespec
	_ = unix.ClockGettime(unix.CLOCK_REALTIME, &now)

	return []telemetry.Sample{
		gauge("system_time_seconds", float64(now.Sec)+float64(now.Nsec)/1e9, nil),
		gauge("system_timex_sync_status", synchronized, nil),
		gauge("system_timex_offset_seconds", float64(timex.Offset)*offsetScale, nil),
		gauge("system_timex_maximum_error_seconds", float64(timex.Maxerror)*1e-6, nil),
		gauge("system_timex_estimated_error_seconds", float64(timex.Esterror)*1e-6, nil),
		gauge("system_timex_frequency_adjustment_ratio", 1+float64(timex.Freq)/65536/1e6, nil),
		gauge("system_timex_loop_time_constant", float64(timex.Constant), nil),
		gauge("system_timex_tick_seconds", float64(timex.Tick)*1e-6, nil),
		gauge("system_timex_tai_offset_seconds", float64(timex.Tai), nil),
		gauge("system_timex_status", float64(timex.Status), nil),
		gauge("system_kernel_info", 1, telemetry.Labels{
			"release": unix.ByteSliceToString(name.Release[:]),
			"version": unix.ByteSliceToString(name.Version[:]),
			"machine": unix.ByteSliceToString(name.Machine[:]),
			"os":      operatingSystemName(),
		}),
	}, nil
}

// operatingSystemName reads PRETTY_NAME from os-release.
func operatingSystemName() string {
	lines, _ := readFields("/etc/os-release")
	for _, fields := range lines {
		if value, found := strings.CutPrefix(strings.Join(fields, " "), "PRETTY_NAME="); found {
			return strings.Trim(value, `"`)
		}
	}
	return ""
}

// sumNumbers adds every field as a number.
func sumNumbers(fields []string, source string) (float64, error) {
	total := 0.0
	for _, field := range fields {
		value, err := parseNumber(field, source)
		if err != nil {
			return 0, err
		}
		total += value
	}
	return total, nil
}
