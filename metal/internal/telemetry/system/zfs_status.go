package system

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// deviceErrorKinds are the error columns of zpool status, in order.
var deviceErrorKinds = []string{"read", "write", "checksum"}

// zpool status prints scan times as in "Sun Oct 11 00:24:24 2026".
const scanTimeLayout = "Mon Jan _2 15:04:05 2006"

var (
	// "scrub repaired 0B in 00:00:23 with 0 errors on Sun Oct 11 00:24:24 2026"
	// "resilvered 1.20G in 00:00:10 with 0 errors on Sun Oct 11 00:24:24 2026"
	scanFinished = regexp.MustCompile(`^(scrub repaired|resilvered) (\S+) in \S+ with (\d+) errors on (.+)$`)
	// "scrub in progress since Sun Oct 11 00:24:01 2026" and the resilver form.
	scanRunning = regexp.MustCompile(`^(scrub|resilver) in progress since`)
	// "scrub canceled on ..." and "scrub paused since ...".
	scanStopped = regexp.MustCompile(`^(scrub|resilver) (canceled|paused)`)
	// "0B repaired, 4.88% done, 00:01:40 to go"
	scanProgress = regexp.MustCompile(`([\d.]+)% done`)
	// "errors: 3 data errors, use '-v' for a list"
	dataErrors = regexp.MustCompile(`^errors: (\d+) data errors`)
	// "1.20G", "512K", or "0B"
	sizeValue = regexp.MustCompile(`^([\d.]+)([KMGTPE]?)B?$`)
)

// parsePoolStatus reads the output of zpool status -p -s for one pool.
func parsePoolStatus(pool, output string, location *time.Location) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	maximum := make([]float64, len(deviceErrorKinds))
	inConfiguration := false
	scanFunction := ""
	errorsFound := 0.0

	for _, line := range strings.Split(output, "\n") {
		trimmed := strings.TrimSpace(line)
		fields := strings.Fields(trimmed)
		switch {
		case strings.HasPrefix(trimmed, "scan:"):
			scan, function := parseScanLine(pool, strings.TrimSpace(strings.TrimPrefix(trimmed, "scan:")), location)
			samples = append(samples, scan...)
			scanFunction = function
		case scanFunction != "" && scanProgress.MatchString(trimmed):
			percent, _ := strconv.ParseFloat(scanProgress.FindStringSubmatch(trimmed)[1], 64)
			samples = append(samples, gauge("system_zfs_scan_progress_percent", percent, telemetry.Labels{"pool": pool, "function": scanFunction}))
		case len(fields) > 0 && fields[0] == "NAME":
			inConfiguration, scanFunction = true, ""
		case strings.HasPrefix(trimmed, "errors:"):
			inConfiguration = false
			if match := dataErrors.FindStringSubmatch(trimmed); match != nil {
				errorsFound, _ = strconv.ParseFloat(match[1], 64)
			}
		case len(fields) == 0 || strings.HasSuffix(fields[0], ":"):
			inConfiguration = false
		case inConfiguration && len(fields) >= 5:
			device, err := deviceSamples(pool, fields, maximum)
			if err != nil {
				return nil, err
			}
			samples = append(samples, device...)
		}
	}

	samples = append(samples, gauge("system_zfs_pool_data_errors", errorsFound, telemetry.Labels{"pool": pool}))
	for index, kind := range deviceErrorKinds {
		samples = append(samples, gauge("system_zfs_device_errors_maximum", maximum[index], telemetry.Labels{"pool": pool, "kind": kind}))
	}
	return samples, nil
}

// deviceSamples reads one configuration row: NAME STATE READ WRITE CKSUM SLOW.
func deviceSamples(pool string, fields []string, maximum []float64) ([]telemetry.Sample, error) {
	device := fields[0]
	samples := []telemetry.Sample{
		gauge("system_zfs_device_state", 1, telemetry.Labels{"pool": pool, "device": device, "state": strings.ToLower(fields[1])}),
	}
	for index, kind := range deviceErrorKinds {
		value, err := parseNumber(fields[2+index], "zpool status")
		if err != nil {
			return nil, err
		}
		maximum[index] = max(maximum[index], value)
		samples = append(samples, gauge("system_zfs_device_errors", value, telemetry.Labels{"pool": pool, "device": device, "kind": kind}))
	}
	// The pool row prints "-" for slow I/Os.
	if len(fields) > 5 {
		if slow, err := strconv.ParseFloat(fields[5], 64); err == nil {
			samples = append(samples, gauge("system_zfs_device_slow_ios", slow, telemetry.Labels{"pool": pool, "device": device}))
		}
	}
	return samples, nil
}

// parseScanLine reads the first line of the scan section and returns the
// function it describes when a progress line may follow.
func parseScanLine(pool, line string, location *time.Location) ([]telemetry.Sample, string) {
	state := func(function, value string) telemetry.Sample {
		return gauge("system_zfs_scan_state", 1, telemetry.Labels{"pool": pool, "function": function, "state": value})
	}

	if match := scanFinished.FindStringSubmatch(line); match != nil {
		function := "scrub"
		if match[1] == "resilvered" {
			function = "resilver"
		}
		labels := telemetry.Labels{"pool": pool, "function": function}
		errors, _ := strconv.ParseFloat(match[3], 64)
		samples := []telemetry.Sample{
			state(function, "finished"),
			gauge("system_zfs_scan_errors", errors, labels),
		}
		if repaired, err := parseSize(match[2]); err == nil {
			samples = append(samples, gauge("system_zfs_scan_repaired_bytes", repaired, labels))
		}
		if finished, err := time.ParseInLocation(scanTimeLayout, strings.Join(strings.Fields(match[4]), " "), location); err == nil {
			samples = append(samples, gauge("system_zfs_scan_end_timestamp_seconds", float64(finished.Unix()), labels))
		}
		return samples, ""
	}
	if match := scanRunning.FindStringSubmatch(line); match != nil {
		return []telemetry.Sample{state(match[1], "running")}, match[1]
	}
	if match := scanStopped.FindStringSubmatch(line); match != nil {
		// A paused scan prints its progress; a canceled one does not.
		return []telemetry.Sample{state(match[1], match[2])}, match[1]
	}
	return []telemetry.Sample{state("none", "none")}, ""
}

// parseSize reads a zpool size such as "1.20G" or "0B" in bytes.
func parseSize(text string) (float64, error) {
	match := sizeValue.FindStringSubmatch(text)
	if match == nil {
		return 0, fmt.Errorf("parse size %q", text)
	}
	value, err := strconv.ParseFloat(match[1], 64)
	if err != nil {
		return 0, err
	}
	exponent := strings.Index("KMGTPE", match[2]) + 1
	if match[2] == "" {
		exponent = 0
	}
	for range exponent {
		value *= 1024
	}
	return value, nil
}
