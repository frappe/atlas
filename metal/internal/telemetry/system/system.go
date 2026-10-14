// Package system collects host metrics from /proc, /sys, ZFS, and systemd.
package system

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/telemetry"
)

// Exclude temporary VM interfaces from permanent metric series.
var ignoredInterfaces = regexp.MustCompile(`^(lo|vh-\d+|vg-\d+|veth.*|tap.*)$`)

// Config selects the host paths and services to read.
type Config struct {
	ProcRoot          string
	SysRoot           string
	DevRoot           string
	Pool              string
	RestartCountUnits []string
	Systemd           UnitReader
	// Output runs a host command, such as zpool or ethtool. Tests replace it.
	Output func(ctx context.Context, name string, args ...string) (string, error)
}

// UnitReader reads systemd unit states.
type UnitReader interface {
	UnitStates(ctx context.Context, patterns []string) ([]platform.UnitState, error)
	UnitRestarts(ctx context.Context, patterns []string) (map[string]uint32, error)
}

// Register adds every host collector to registry.
func Register(registry *telemetry.Registry, configuration Config) {
	if configuration.ProcRoot == "" {
		configuration.ProcRoot = "/proc"
	}
	if configuration.SysRoot == "" {
		configuration.SysRoot = "/sys"
	}
	if configuration.DevRoot == "" {
		configuration.DevRoot = "/dev"
	}
	if configuration.Output == nil {
		configuration.Output = platform.Output
	}
	proc, sys := configuration.ProcRoot, configuration.SysRoot

	registry.Register("cpu", &cpuCollector{procRoot: proc, sysRoot: sys})
	registry.Register("kernel", kernelCollector{procRoot: proc})
	registry.Register("processes", processCollector{procRoot: proc})
	registry.Register("time", timeCollector{})
	registry.Register("memory", memoryCollector{procRoot: proc})
	registry.Register("disk", diskCollector{procRoot: proc, sysRoot: sys})
	registry.Register("filesystem", filesystemCollector{procRoot: proc})
	registry.Register("mdadm", mdadmCollector{procRoot: proc, sysRoot: sys})
	registry.Register("network", networkCollector{procRoot: proc, sysRoot: sys})
	registry.Register("netstat", netstatCollector{procRoot: proc})
	registry.Register("ethtool", ethtoolCollector{sysRoot: sys, output: configuration.Output})
	registry.Register("hardware", hardwareCollector{sysRoot: sys})
	registry.Register("nvme", nvmeCollector{sysRoot: sys, devRoot: configuration.DevRoot})
	registry.Register("smart", &smartCollector{sysRoot: sys, run: platform.CombinedOutput})
	registry.Register("zfs", zfsCollector{pool: configuration.Pool, procRoot: proc, output: configuration.Output})
	if configuration.Systemd != nil {
		registry.Register("systemd", systemdCollector{restartCountUnits: configuration.RestartCountUnits, reader: configuration.Systemd})
	}
}

// gauge returns one sample without a time; the registry stamps the snapshot time.
func gauge(metric string, value float64, labels telemetry.Labels) telemetry.Sample {
	return telemetry.Sample{Metric: metric, Value: value, Labels: labels}
}

func readFields(path string) ([][]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	var lines [][]string
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		if fields := strings.Fields(scanner.Text()); len(fields) > 0 {
			lines = append(lines, fields)
		}
	}
	return lines, scanner.Err()
}

func readNumber(path string) (float64, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseFloat(strings.TrimSpace(string(content)), 64)
}

func readText(path string) string {
	content, _ := os.ReadFile(path)
	return strings.TrimSpace(string(content))
}

func parseNumber(field, source string) (float64, error) {
	value, err := strconv.ParseFloat(field, 64)
	if err != nil {
		return 0, fmt.Errorf("parse %s: %w", filepath.Base(source), err)
	}
	return value, nil
}

func numberFiles(directory, metric string, labels telemetry.Labels) []telemetry.Sample {
	entries, _ := os.ReadDir(directory)

	var samples []telemetry.Sample
	for _, entry := range entries {
		if !entry.Type().IsRegular() {
			continue
		}
		value, err := readNumber(filepath.Join(directory, entry.Name()))
		if err != nil {
			continue
		}
		fieldLabels := telemetry.Labels{"field": entry.Name()}
		for name, labelValue := range labels {
			fieldLabels[name] = labelValue
		}
		samples = append(samples, gauge(metric, value, fieldLabels))
	}
	return samples
}
