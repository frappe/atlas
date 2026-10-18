package system

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

const sectorBytes = 512

// ignoredDisks are virtual devices. zd* are VM volumes, one per VM.
var ignoredDisks = regexp.MustCompile(`^(loop|ram|zram|zd|sr|nbd|fd)\d*`)

var readOnlyOption = regexp.MustCompile(`(^|,)ro(,|$)`)

// filesystemTypes are the mounted filesystems worth reporting. ZFS datasets
// here hold host state; VM disks are volumes and never appear as mounts.
var filesystemTypes = []string{"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "zfs", "tmpfs"}

// diskCounters maps /proc/diskstats columns, after the device name, to metrics.
var diskCounters = []struct {
	column int
	metric string
	scale  float64
}{
	{0, "system_disk_reads_completed_total", 1},
	{1, "system_disk_reads_merged_total", 1},
	{2, "system_disk_read_bytes_total", sectorBytes},
	{3, "system_disk_read_time_seconds_total", 0.001},
	{4, "system_disk_writes_completed_total", 1},
	{5, "system_disk_writes_merged_total", 1},
	{6, "system_disk_written_bytes_total", sectorBytes},
	{7, "system_disk_write_time_seconds_total", 0.001},
	{8, "system_disk_io_in_progress", 1},
	{9, "system_disk_io_time_seconds_total", 0.001},
	{10, "system_disk_io_weighted_time_seconds_total", 0.001},
	{11, "system_disk_discards_completed_total", 1},
	{12, "system_disk_discards_merged_total", 1},
	{13, "system_disk_discarded_bytes_total", sectorBytes},
	{14, "system_disk_discard_time_seconds_total", 0.001},
	{15, "system_disk_flush_requests_total", 1},
	{16, "system_disk_flush_requests_time_seconds_total", 0.001},
}

type diskCollector struct {
	procRoot string
	sysRoot  string
}

// Collect reads available /proc/diskstats counters for whole disks.
func (collector diskCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "diskstats")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines {
		if len(fields) < 14 || !collector.isReported(fields[2]) {
			continue
		}
		labels := telemetry.Labels{"device": fields[2]}
		for _, counter := range diskCounters {
			if 3+counter.column >= len(fields) {
				break
			}
			value, err := parseNumber(fields[3+counter.column], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, gauge(counter.metric, value*counter.scale, labels))
		}
		samples = append(samples, collector.scsiSamples(fields[2])...)
	}
	return samples, nil
}

// Exclude ioerr_cnt because smartctl commands raise it.
func (collector diskCollector) scsiSamples(device string) []telemetry.Sample {
	directory := filepath.Join(collector.sysRoot, "block", device, "device")
	state := readText(filepath.Join(directory, "state"))
	if state == "" {
		return nil
	}

	samples := []telemetry.Sample{gauge("system_disk_scsi_state", 1, telemetry.Labels{"device": device, "state": state})}
	if timeouts, err := strconv.ParseUint(readText(filepath.Join(directory, "iotmo_cnt")), 0, 64); err == nil {
		samples = append(samples, gauge("system_disk_scsi_timeouts_total", float64(timeouts), telemetry.Labels{"device": device}))
	}
	return samples
}

// isReported keeps whole disks: a partition has no /sys/block entry.
func (collector diskCollector) isReported(device string) bool {
	if ignoredDisks.MatchString(device) {
		return false
	}
	_, err := os.Stat(filepath.Join(collector.sysRoot, "block", device))
	return err == nil
}

type filesystemCollector struct {
	procRoot string
}

type mount struct {
	device, path, filesystemType string
	isReadOnly                   bool
}

// Collect reports filesystem space, inodes, and per-mount errors.
func (collector filesystemCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	mounts, err := collector.mounts()
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, mounted := range mounts {
		labels := telemetry.Labels{"mountpoint": mounted.path, "device": mounted.device, "type": mounted.filesystemType}
		var stat unix.Statfs_t
		if err := unix.Statfs(mounted.path, &stat); err != nil {
			samples = append(samples, gauge("system_filesystem_device_error", 1, labels))
			continue
		}
		samples = append(samples, gauge("system_filesystem_device_error", 0, labels))
		samples = append(samples, filesystemSamples(mounted, stat, labels)...)
	}
	return samples, nil
}

func filesystemSamples(mounted mount, stat unix.Statfs_t, labels telemetry.Labels) []telemetry.Sample {
	blockSize := float64(stat.Bsize)
	size, free, available := float64(stat.Blocks)*blockSize, float64(stat.Bfree)*blockSize, float64(stat.Bavail)*blockSize
	readOnly := 0.0
	if mounted.isReadOnly {
		readOnly = 1
	}

	samples := []telemetry.Sample{
		gauge("system_filesystem_size_bytes", size, labels),
		gauge("system_filesystem_free_bytes", free, labels),
		gauge("system_filesystem_available_bytes", available, labels),
		gauge("system_filesystem_inodes", float64(stat.Files), labels),
		gauge("system_filesystem_inodes_free", float64(stat.Ffree), labels),
		gauge("system_filesystem_read_only", readOnly, labels),
	}
	if mounted.path != "/" || size == 0 {
		return samples
	}
	return append(samples,
		gauge("system_disk_total_bytes", size, nil),
		gauge("system_disk_used_bytes", size-free, nil),
		gauge("system_disk_free_bytes", available, nil),
		gauge("system_disk_usage_percent", (size-free)/(size-free+available)*100, nil),
	)
}

// Exclude temporary session and credential mounts.
func (collector filesystemCollector) mounts() ([]mount, error) {
	lines, err := readFields(filepath.Join(collector.procRoot, "self", "mounts"))
	if err != nil {
		return nil, err
	}

	var mounts []mount
	seen := make(map[string]bool)
	for _, fields := range lines {
		if len(fields) < 4 || !slices.Contains(filesystemTypes, fields[2]) || seen[fields[1]] {
			continue
		}
		if fields[2] == "tmpfs" && (strings.HasPrefix(fields[1], "/run/user/") || strings.HasPrefix(fields[1], "/run/credentials/")) {
			continue
		}
		seen[fields[1]] = true
		mounts = append(mounts, mount{
			device: fields[0], path: fields[1], filesystemType: fields[2],
			isReadOnly: readOnlyOption.MatchString(fields[3]),
		})
	}
	return mounts, nil
}

// mdadmLine matches "md2 : active raid1 sdb4[1] sda4[0]".
var mdadmLine = regexp.MustCompile(`^(md\S+)\s*:\s*(\S+)\s+(?:\((?:auto-)?read-only\)\s+)?(\S+)?`)

// mdadmStatus matches "931388736 blocks super 1.2 [2/2] [UU]".
var mdadmStatus = regexp.MustCompile(`^\s*(\d+) blocks.*\[(\d+)/(\d+)\]`)

// mdadmProgress matches "[=>....]  recovery = 8.5% (... ) finish=..." and resync lines.
var mdadmProgress = regexp.MustCompile(`(recovery|resync|check|reshape)\s*=\s*([\d.]+)%`)

type mdadmCollector struct {
	procRoot string
	sysRoot  string
}

// Collect reports software RAID state, members, health, and recovery progress.
func (collector mdadmCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	content, err := os.ReadFile(filepath.Join(collector.procRoot, "mdstat"))
	if err != nil {
		return nil, nil
	}

	var samples []telemetry.Sample
	var device string
	for _, line := range strings.Split(string(content), "\n") {
		if match := mdadmLine.FindStringSubmatch(line); match != nil {
			device = match[1]
			failed := float64(strings.Count(line, "(F)"))
			spare := float64(strings.Count(line, "(S)"))
			members := float64(strings.Count(line, "["))
			samples = append(samples,
				gauge("system_md_state", 1, telemetry.Labels{"device": device, "state": match[2], "level": match[3]}),
				gauge("system_md_disks", failed, telemetry.Labels{"device": device, "state": "failed"}),
				gauge("system_md_disks", spare, telemetry.Labels{"device": device, "state": "spare"}),
				gauge("system_md_disks", members-failed-spare, telemetry.Labels{"device": device, "state": "active"}),
			)
			samples = append(samples, collector.arraySamples(device)...)
			continue
		}
		if device == "" {
			continue
		}
		if match := mdadmStatus.FindStringSubmatch(line); match != nil {
			blocks, _ := parseNumber(match[1], "mdstat")
			required, _ := parseNumber(match[2], "mdstat")
			running, _ := parseNumber(match[3], "mdstat")
			labels := telemetry.Labels{"device": device}
			samples = append(samples,
				gauge("system_md_blocks", blocks, labels),
				gauge("system_md_disks_required", required, labels),
				gauge("system_md_disks_running", running, labels),
			)
		}
		if match := mdadmProgress.FindStringSubmatch(line); match != nil {
			percent, err := parseNumber(match[2], "mdstat")
			if err != nil {
				return samples, fmt.Errorf("parse mdstat progress: %w", err)
			}
			samples = append(samples, gauge("system_md_sync_percent", percent, telemetry.Labels{"device": device, "action": match[1]}))
		}
	}
	return samples, nil
}

// mismatch_cnt counts differing sectors; member errors count repaired read errors.
func (collector mdadmCollector) arraySamples(device string) []telemetry.Sample {
	directory := filepath.Join(collector.sysRoot, "block", device, "md")
	labels := telemetry.Labels{"device": device}

	var samples []telemetry.Sample
	for _, file := range []string{"degraded", "mismatch_cnt"} {
		if value, err := readNumber(filepath.Join(directory, file)); err == nil {
			samples = append(samples, gauge("system_md_"+file, value, labels))
		}
	}
	if action := readText(filepath.Join(directory, "sync_action")); action != "" {
		samples = append(samples, gauge("system_md_sync_action", 1, telemetry.Labels{"device": device, "action": action}))
	}
	if state := readText(filepath.Join(directory, "array_state")); state != "" {
		samples = append(samples, gauge("system_md_array_state", 1, telemetry.Labels{"device": device, "state": state}))
	}

	members, _ := filepath.Glob(filepath.Join(directory, "dev-*"))
	for _, member := range members {
		memberLabels := telemetry.Labels{"device": device, "member": strings.TrimPrefix(filepath.Base(member), "dev-")}
		if corrected, err := readNumber(filepath.Join(member, "errors")); err == nil {
			samples = append(samples, gauge("system_md_member_corrected_errors", corrected, memberLabels))
		}
		if state := readText(filepath.Join(member, "state")); state != "" {
			samples = append(samples, gauge("system_md_member_state", 1, telemetry.Labels{"device": device, "member": memberLabels["member"], "state": state}))
		}
		if badBlocks, err := readFields(filepath.Join(member, "bad_blocks")); err == nil {
			samples = append(samples, gauge("system_md_member_bad_block_ranges", float64(len(badBlocks)), memberLabels))
		}
	}
	return samples
}
