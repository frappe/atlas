package system

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// poolColumns are the zpool list columns after the name, in order.
var poolColumns = []string{"size", "allocated", "free", "fragmentation", "capacity"}

// skippedKstats are kstat files that are benchmarks, logs, or tables.
var skippedKstats = []string{"chksum_bench", "fletcher_4_bench", "vdev_raidz_bench", "dbgmsg", "dbufs", "fm", "import_progress", "simd", "zstd"}

type zfsCollector struct {
	pool     string
	procRoot string
	output   func(ctx context.Context, name string, args ...string) (string, error)
}

// Collect reads pool space and health, each device's state and errors, pool
// I/O counters, and every module-wide kstat, such as the ARC and the ZIL.
func (collector zfsCollector) Collect(ctx context.Context) ([]telemetry.Sample, error) {
	if collector.pool == "" {
		return nil, nil
	}

	samples, err := collector.poolSpace(ctx)
	if err != nil {
		return nil, err
	}
	devices, err := collector.devices(ctx)
	if err != nil {
		return samples, err
	}
	samples = append(samples, devices...)
	samples = append(samples, collector.poolKstats()...)

	kstats, err := collector.moduleKstats()
	return append(samples, kstats...), err
}

func (collector zfsCollector) poolSpace(ctx context.Context) ([]telemetry.Sample, error) {
	output, err := collector.output(ctx, "zpool", "list", "-Hp", "-o", "size,allocated,free,fragmentation,capacity,health", collector.pool)
	if err != nil {
		return nil, err
	}
	fields := strings.Fields(output)
	if len(fields) != len(poolColumns)+1 {
		return nil, fmt.Errorf("parse zpool list: unexpected output %q", output)
	}

	labels := telemetry.Labels{"pool": collector.pool}
	var samples []telemetry.Sample
	for index, column := range poolColumns {
		// Fragmentation is "-" for a pool that cannot report it.
		if fields[index] == "-" {
			continue
		}
		value, err := parseNumber(strings.TrimSuffix(fields[index], "%"), "zpool list")
		if err != nil {
			return nil, err
		}
		metric := "system_zfs_pool_" + column + "_bytes"
		if column == "fragmentation" || column == "capacity" {
			metric = "system_zfs_pool_" + column + "_percent"
		}
		samples = append(samples, gauge(metric, value, labels))
	}

	health := fields[len(poolColumns)]
	healthy := 0.0
	if health == "ONLINE" {
		healthy = 1
	}
	return append(samples,
		gauge("system_zfs_pool_healthy", healthy, labels),
		gauge("system_zfs_pool_state", 1, telemetry.Labels{"pool": collector.pool, "state": strings.ToLower(health)}),
	), nil
}

// devices reads zpool status: each device's state, errors, and slow I/Os, the
// last or running scrub or resilver, and the count of damaged data blocks.
func (collector zfsCollector) devices(ctx context.Context) ([]telemetry.Sample, error) {
	output, err := collector.output(ctx, "zpool", "status", "-p", "-s", collector.pool)
	if err != nil {
		return nil, err
	}
	return parsePoolStatus(collector.pool, output, time.Local)
}

// poolKstats reads the pool's I/O counters. objset-* files describe single
// datasets, one per VM disk, so they are not read.
func (collector zfsCollector) poolKstats() []telemetry.Sample {
	directory := filepath.Join(collector.procRoot, "spl", "kstat", "zfs", collector.pool)
	samples, _ := readKstat(filepath.Join(directory, "iostats"), "system_zfs_pool_kstat", telemetry.Labels{"pool": collector.pool, "kstat": "iostats"})
	if state := readText(filepath.Join(directory, "state")); state != "" {
		samples = append(samples, gauge("system_zfs_pool_kstat_state", 1, telemetry.Labels{"pool": collector.pool, "state": strings.ToLower(state)}))
	}
	return samples
}

// moduleKstats reads every module-wide kstat file in "name type data" form,
// such as arcstats, zil, dmu_tx, abdstats, and zfetchstats.
func (collector zfsCollector) moduleKstats() ([]telemetry.Sample, error) {
	directory := filepath.Join(collector.procRoot, "spl", "kstat", "zfs")
	entries, err := os.ReadDir(directory)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, entry := range entries {
		if entry.IsDir() || slices.Contains(skippedKstats, entry.Name()) {
			continue
		}
		kstat, err := readKstat(filepath.Join(directory, entry.Name()), "system_zfs_kstat", telemetry.Labels{"kstat": entry.Name()})
		if err != nil {
			return samples, err
		}
		samples = append(samples, kstat...)
	}
	return samples, nil
}

// readKstat reads a kstat file whose second line is "name type data". A file in
// another form is skipped. Type 7 is a string, so it is not a number.
func readKstat(path, metric string, labels telemetry.Labels) ([]telemetry.Sample, error) {
	lines, err := readFields(path)
	if err != nil || len(lines) < 2 || strings.Join(lines[1], " ") != "name type data" {
		return nil, nil
	}

	var samples []telemetry.Sample
	for _, fields := range lines[2:] {
		if len(fields) != 3 || fields[1] == "7" {
			continue
		}
		value, err := parseNumber(fields[2], path)
		if err != nil {
			return nil, err
		}
		fieldLabels := telemetry.Labels{"field": fields[0]}
		for name, labelValue := range labels {
			fieldLabels[name] = labelValue
		}
		samples = append(samples, gauge(metric, value, fieldLabels))
	}
	return samples, nil
}
