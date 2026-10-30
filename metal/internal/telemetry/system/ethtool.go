package system

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type ethtoolCollector struct {
	sysRoot string
	output  func(ctx context.Context, name string, args ...string) (string, error)
}

// Collect reports every driver statistic of each physical NIC. Drivers count
// ring, queue, and discard events that /proc/net/dev never shows.
func (collector ethtoolCollector) Collect(ctx context.Context) ([]telemetry.Sample, error) {
	devices, _ := filepath.Glob(filepath.Join(collector.sysRoot, "class", "net", "*", "device"))

	var samples []telemetry.Sample
	var failures []error
	for _, device := range devices {
		name := filepath.Base(filepath.Dir(device))
		if ignoredInterfaces.MatchString(name) {
			continue
		}
		if _, err := os.Stat(device); err != nil {
			continue
		}
		output, err := collector.output(ctx, "ethtool", "-S", name)
		var exitError *exec.ExitError
		if errors.As(err, &exitError) && exitError.ExitCode() == 94 {
			// ethtool exits 94 when the driver has no statistics, as for a USB NIC.
			continue
		}
		if err != nil {
			failures = append(failures, err)
			continue
		}
		samples = append(samples, parseEthtoolStatistics(name, output)...)
	}
	return samples, errors.Join(failures...)
}

// parseEthtoolStatistics reads "     rx_discards: 12" lines. A driver such as
// bnxt prefixes queue statistics, as in "[0]: rx_ucast_packets: 1".
func parseEthtoolStatistics(name, output string) []telemetry.Sample {
	var samples []telemetry.Sample
	for _, line := range strings.Split(output, "\n") {
		separator := strings.LastIndex(line, ":")
		if separator < 0 {
			continue
		}
		value, err := strconv.ParseFloat(strings.TrimSpace(line[separator+1:]), 64)
		if err != nil {
			continue
		}
		field := strings.Join(strings.Fields(strings.ReplaceAll(line[:separator], ":", "")), "_")
		samples = append(samples, gauge("system_ethtool", value, telemetry.Labels{"interface": name, "field": field}))
	}
	return samples
}
