package system

import (
	"context"
	"path/filepath"
	"slices"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type memoryCollector struct {
	procRoot string
}

// Collect reports every /proc/meminfo field and every /proc/vmstat counter,
// plus the memory metrics that every machine sends.
func (collector memoryCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	info, err := collector.meminfo()
	if err != nil {
		return nil, err
	}

	samples := memoryUsage(info)
	for _, field := range memoryInfoFields {
		if value, found := info[field]; found {
			samples = append(samples, gauge("system_memory_info", value, telemetry.Labels{"field": field}))
		}
	}

	counters, err := collector.vmstat()
	return append(samples, counters...), err
}

// meminfo returns each field in bytes. Fields such as HugePages_Total have no
// unit and stay counts.
func (collector memoryCollector) meminfo() (map[string]float64, error) {
	path := filepath.Join(collector.procRoot, "meminfo")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	values := make(map[string]float64, len(lines))
	for _, fields := range lines {
		if len(fields) < 2 {
			continue
		}
		value, err := parseNumber(fields[1], path)
		if err != nil {
			return nil, err
		}
		if len(fields) == 3 && fields[2] == "kB" {
			value *= 1024
		}
		// "Active(anon):" becomes Active_anon, as node exporter names it.
		field := strings.NewReplacer(":", "", "(", "_", ")", "").Replace(fields[0])
		values[field] = value
	}
	return values, nil
}

// memoryUsage reports the memory metrics of the telemetry contract.
func memoryUsage(info map[string]float64) []telemetry.Sample {
	if info["MemTotal"] == 0 {
		return nil
	}
	return []telemetry.Sample{
		gauge("system_memory_total_bytes", info["MemTotal"], nil),
		gauge("system_memory_available_bytes", info["MemAvailable"], nil),
		gauge("system_memory_swap_used_bytes", info["SwapTotal"]-info["SwapFree"], nil),
	}
}

// memoryInfoFields are the /proc/meminfo fields that the host charts read. The others stay on the host.
var memoryInfoFields = []string{
	"MemTotal", "MemFree", "Buffers", "Cached", "SwapCached", "SwapTotal", "SwapFree",
	"Active", "Inactive", "Active_anon", "Inactive_anon", "Active_file", "Inactive_file", "Unevictable", "Mlocked",
	"Dirty", "Writeback", "WritebackTmp", "NFS_Unstable", "Bounce", "AnonPages", "AnonHugePages", "Mapped",
	"Shmem", "ShmemHugePages", "ShmemPmdMapped", "Slab", "SReclaimable", "SUnreclaim", "KernelStack",
	"PageTables", "Percpu", "CommitLimit", "Committed_AS", "VmallocTotal", "VmallocUsed", "VmallocChunk",
	"HardwareCorrupted", "HugePages_Total", "HugePages_Free", "HugePages_Rsvd", "HugePages_Surp",
	"Hugepagesize", "DirectMap4k", "DirectMap2M", "DirectMap1G",
}

// vmstatFields are the /proc/vmstat counters that the host charts read. The others stay on the host.
var vmstatFields = []string{"pgpgin", "pgpgout", "pswpin", "pswpout", "pgfault", "pgmajfault", "oom_kill"}

// vmstat reports the vmstatFields counters of /proc/vmstat.
func (collector memoryCollector) vmstat() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "vmstat")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines {
		if len(fields) < 2 || !slices.Contains(vmstatFields, fields[0]) {
			continue
		}
		value, err := parseNumber(fields[1], path)
		if err != nil {
			return nil, err
		}
		samples = append(samples, gauge("system_vmstat", value, telemetry.Labels{"field": fields[0]}))
	}
	return samples, nil
}
