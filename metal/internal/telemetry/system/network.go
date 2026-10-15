package system

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// interfaceColumns are the /proc/net/dev columns after the interface name.
var interfaceColumns = []string{
	"receive_bytes", "receive_packets", "receive_errors", "receive_drops", "receive_fifo_errors",
	"receive_frame_errors", "receive_compressed", "receive_multicast",
	"transmit_bytes", "transmit_packets", "transmit_errors", "transmit_drops", "transmit_fifo_errors",
	"transmit_collisions", "transmit_carrier_errors", "transmit_compressed",
}

// linkAttributes are numeric sysfs attributes of a network interface.
var linkAttributes = map[string]string{
	"mtu": "system_network_mtu_bytes", "carrier": "system_network_carrier",
	"carrier_changes": "system_network_carrier_changes_total", "carrier_up_count": "system_network_carrier_up_changes_total",
	"carrier_down_count": "system_network_carrier_down_changes_total", "tx_queue_len": "system_network_transmit_queue_length",
	"dormant": "system_network_dormant", "flags": "system_network_flags", "iflink": "system_network_iface_link",
}

// softnetColumns are the hexadecimal /proc/net/softnet_stat columns of each CPU.
var softnetColumns = map[int]string{
	0: "system_network_softnet_processed_total", 1: "system_network_softnet_dropped_total",
	2: "system_network_softnet_time_squeezed_total", 9: "system_network_softnet_received_rps_total",
	10: "system_network_softnet_flow_limit_total",
}

type networkCollector struct {
	procRoot string
	sysRoot  string
}

// Collect reports network counters, retaining partial results on read errors.
func (collector networkCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	var failures []error
	for _, read := range []func() ([]telemetry.Sample, error){
		collector.interfaces, collector.softnet, collector.conntrack, collector.arp, collector.udpQueues, collector.bonds,
	} {
		collected, err := read()
		samples = append(samples, collected...)
		failures = append(failures, err)
	}
	return samples, errors.Join(failures...)
}

func (collector networkCollector) interfaces() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "net", "dev")
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines {
		name, isInterface := strings.CutSuffix(fields[0], ":")
		if !isInterface || len(fields) < 17 || ignoredInterfaces.MatchString(name) {
			continue
		}
		labels := telemetry.Labels{"interface": name}
		for column, counter := range interfaceColumns {
			value, err := parseNumber(fields[1+column], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, gauge("system_network_"+counter+"_total", value, labels))
		}
		samples = append(samples, collector.link(name, labels)...)
	}
	return samples, nil
}

// sysfs separates driver error counters that /proc/net/dev combines.
func (collector networkCollector) link(name string, labels telemetry.Labels) []telemetry.Sample {
	directory := filepath.Join(collector.sysRoot, "class", "net", name)
	operationalState := readText(filepath.Join(directory, "operstate"))
	up := 0.0
	if operationalState == "up" {
		up = 1
	}

	samples := []telemetry.Sample{
		gauge("system_network_up", up, labels),
		gauge("system_network_info", 1, telemetry.Labels{
			"interface": name, "address": readText(filepath.Join(directory, "address")),
			"duplex": readText(filepath.Join(directory, "duplex")), "operstate": operationalState,
			"driver": readLinkName(filepath.Join(directory, "device", "driver")),
		}),
	}
	for file, metric := range linkAttributes {
		if value, err := readNumber(filepath.Join(directory, file)); err == nil {
			samples = append(samples, gauge(metric, value, labels))
		}
	}
	// speed is in megabits per second, and -1 when the link is down.
	if megabits, err := readNumber(filepath.Join(directory, "speed")); err == nil && megabits >= 0 {
		samples = append(samples, gauge("system_network_speed_bytes", megabits*125000, labels))
	}
	for _, statistic := range []string{"rx_missed_errors", "rx_over_errors", "rx_crc_errors", "rx_length_errors", "tx_aborted_errors", "tx_heartbeat_errors", "tx_window_errors"} {
		if value, err := readNumber(filepath.Join(directory, "statistics", statistic)); err == nil {
			samples = append(samples, gauge("system_network_"+statistic+"_total", value, labels))
		}
	}
	return samples
}

// Softnet drops and squeezes indicate receive processing cannot keep up.
func (collector networkCollector) softnet() ([]telemetry.Sample, error) {
	lines, err := readFields(filepath.Join(collector.procRoot, "net", "softnet_stat"))
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for row, fields := range lines {
		// Newer kernels name the CPU in column 12; older ones list CPUs in order.
		cpu := strconv.Itoa(row)
		if len(fields) > 12 {
			if index, err := strconv.ParseUint(fields[12], 16, 64); err == nil {
				cpu = strconv.FormatUint(index, 10)
			}
		}
		for column, metric := range softnetColumns {
			if column >= len(fields) {
				continue
			}
			value, err := strconv.ParseUint(fields[column], 16, 64)
			if err != nil {
				return nil, fmt.Errorf("parse softnet_stat: %w", err)
			}
			samples = append(samples, gauge(metric, float64(value), telemetry.Labels{"cpu": cpu}))
		}
	}
	return samples, nil
}

func (collector networkCollector) conntrack() ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for file, metric := range map[string]string{
		"nf_conntrack_count": "system_network_conntrack_entries",
		"nf_conntrack_max":   "system_network_conntrack_entries_limit",
	} {
		if value, err := readNumber(filepath.Join(collector.procRoot, "sys", "net", "netfilter", file)); err == nil {
			samples = append(samples, gauge(metric, value, nil))
		}
	}

	lines, err := readFields(filepath.Join(collector.procRoot, "net", "stat", "nf_conntrack"))
	if errors.Is(err, fs.ErrNotExist) || len(lines) < 2 {
		return samples, nil
	}
	if err != nil {
		return samples, err
	}
	header := lines[0]
	totals := make([]float64, len(header))
	for _, fields := range lines[1:] {
		for column := range min(len(fields), len(header)) {
			value, err := strconv.ParseUint(fields[column], 16, 64)
			if err != nil {
				return samples, fmt.Errorf("parse nf_conntrack stat: %w", err)
			}
			totals[column] += float64(value)
		}
	}
	for column, field := range header {
		if field == "entries" {
			continue
		}
		samples = append(samples, gauge("system_network_conntrack_stat_total", totals[column], telemetry.Labels{"field": field}))
	}
	return samples, nil
}

func (collector networkCollector) arp() ([]telemetry.Sample, error) {
	lines, err := readFields(filepath.Join(collector.procRoot, "net", "arp"))
	if err != nil {
		return nil, err
	}

	counts := make(map[string]float64)
	for _, fields := range lines[1:] {
		if len(fields) >= 6 {
			counts[fields[5]]++
		}
	}
	var samples []telemetry.Sample
	for device, count := range counts {
		samples = append(samples, gauge("system_network_arp_entries", count, telemetry.Labels{"device": device}))
	}
	return samples, nil
}

func (collector networkCollector) udpQueues() ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for file, family := range map[string]string{"udp": "v4", "udp6": "v6"} {
		lines, err := readFields(filepath.Join(collector.procRoot, "net", file))
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return samples, err
		}

		var transmit, receive uint64
		for _, fields := range lines[1:] {
			if len(fields) < 5 {
				continue
			}
			transmitHex, receiveHex, found := strings.Cut(fields[4], ":")
			transmitBytes, transmitError := strconv.ParseUint(transmitHex, 16, 64)
			receiveBytes, receiveError := strconv.ParseUint(receiveHex, 16, 64)
			if !found || transmitError != nil || receiveError != nil {
				continue
			}
			transmit += transmitBytes
			receive += receiveBytes
		}
		samples = append(samples,
			gauge("system_network_udp_queue_bytes", float64(transmit), telemetry.Labels{"ip": family, "queue": "tx"}),
			gauge("system_network_udp_queue_bytes", float64(receive), telemetry.Labels{"ip": family, "queue": "rx"}),
		)
	}
	return samples, nil
}

func (collector networkCollector) bonds() ([]telemetry.Sample, error) {
	masters := strings.Fields(readText(filepath.Join(collector.sysRoot, "class", "net", "bonding_masters")))

	var samples []telemetry.Sample
	for _, master := range masters {
		slaves := strings.Fields(readText(filepath.Join(collector.sysRoot, "class", "net", master, "bonding", "slaves")))
		active := 0.0
		for _, slave := range slaves {
			if readText(filepath.Join(collector.sysRoot, "class", "net", slave, "bonding_slave", "mii_status")) == "up" {
				active++
			}
		}
		labels := telemetry.Labels{"master": master}
		samples = append(samples,
			gauge("system_network_bonding_slaves", float64(len(slaves)), labels),
			gauge("system_network_bonding_active", active, labels),
		)
	}
	return samples, nil
}

func readLinkName(path string) string {
	target, err := os.Readlink(path)
	if err != nil {
		return ""
	}
	return filepath.Base(target)
}
