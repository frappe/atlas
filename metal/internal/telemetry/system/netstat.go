package system

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

type netstatCollector struct {
	procRoot string
}

// Collect reports every protocol counter in /proc/net/snmp, netstat, and snmp6,
// and every socket count in sockstat and sockstat6.
func (collector netstatCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for _, file := range []string{"snmp", "netstat"} {
		counters, err := collector.pairedTable(filepath.Join(collector.procRoot, "net", file))
		if err != nil {
			return samples, err
		}
		samples = append(samples, counters...)
	}

	ipv6, err := collector.snmp6()
	if err != nil {
		return samples, err
	}
	samples = append(samples, ipv6...)

	for _, file := range []string{"sockstat", "sockstat6"} {
		sockets, err := collector.sockstat(filepath.Join(collector.procRoot, "net", file))
		if err != nil {
			return samples, err
		}
		samples = append(samples, sockets...)
	}
	return samples, nil
}

// pairedTable reads files where a header line of names is followed by a line
// of values, such as "Tcp: RtoAlgorithm RtoMin" then "Tcp: 1 200".
func (collector netstatCollector) pairedTable(path string) ([]telemetry.Sample, error) {
	lines, err := readFields(path)
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for index := 0; index+1 < len(lines); index += 2 {
		header, values := lines[index], lines[index+1]
		if header[0] != values[0] || len(header) != len(values) {
			continue
		}
		protocol := strings.TrimSuffix(header[0], ":")
		for column, field := range header[1:] {
			value, err := parseNumber(values[column+1], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, gauge("system_netstat", value, telemetry.Labels{"protocol": protocol, "field": field}))
		}
	}
	return samples, nil
}

// snmp6 reads "Ip6InReceives 1234" lines. The protocol prefix ends where the
// field name starts with an upper-case letter after the digit 6.
func (collector netstatCollector) snmp6() ([]telemetry.Sample, error) {
	path := filepath.Join(collector.procRoot, "net", "snmp6")
	lines, err := readFields(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var samples []telemetry.Sample
	for _, fields := range lines {
		if len(fields) < 2 {
			continue
		}
		protocol, field, found := strings.Cut(fields[0], "6")
		if !found {
			continue
		}
		value, err := parseNumber(fields[1], path)
		if err != nil {
			return nil, err
		}
		samples = append(samples, gauge("system_netstat", value, telemetry.Labels{"protocol": protocol + "6", "field": field}))
	}
	return samples, nil
}

// sockstat reads lines such as "TCP: inuse 5 orphan 0 tw 2 alloc 7 mem 1".
// mem counts pages, so it is also reported in bytes.
func (collector netstatCollector) sockstat(path string) ([]telemetry.Sample, error) {
	lines, err := readFields(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	pageSize := float64(os.Getpagesize())
	var samples []telemetry.Sample
	for _, fields := range lines {
		protocol := strings.TrimSuffix(fields[0], ":")
		for index := 1; index+1 < len(fields); index += 2 {
			value, err := parseNumber(fields[index+1], path)
			if err != nil {
				return nil, err
			}
			field := fields[index]
			samples = append(samples, gauge("system_sockstat", value, telemetry.Labels{"protocol": protocol, "field": field}))
			if field == "mem" {
				samples = append(samples, gauge("system_sockstat", value*pageSize, telemetry.Labels{"protocol": protocol, "field": "mem_bytes"}))
			}
		}
	}
	return samples, nil
}
