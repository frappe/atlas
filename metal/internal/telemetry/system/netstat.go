package system

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// netstatFields are the protocol counters that the host charts read. The other counters, and the
// IPv6 counters of snmp6, stay on the host.
var netstatFields = map[string][]string{
	"Ip":    {"Forwarding"},
	"IpExt": {"InOctets", "OutOctets"},
	"Icmp":  {"InMsgs", "OutMsgs", "InErrors"},
	"Udp":   {"InDatagrams", "OutDatagrams", "InErrors", "NoPorts", "RcvbufErrors", "SndbufErrors"},
	"Tcp":   {"InSegs", "OutSegs", "RetransSegs", "InErrs", "CurrEstab", "MaxConn", "ActiveOpens", "PassiveOpens"},
	"TcpExt": {
		"ListenOverflows", "ListenDrops", "TCPSynRetrans", "SyncookiesFailed", "SyncookiesRecv", "SyncookiesSent",
	},
}

type netstatCollector struct {
	procRoot string
}

// Collect reports the netstatFields counters of /proc/net/snmp and netstat, and every socket
// count in sockstat and sockstat6.
func (collector netstatCollector) Collect(context.Context) ([]telemetry.Sample, error) {
	var samples []telemetry.Sample
	for _, file := range []string{"snmp", "netstat"} {
		counters, err := collector.pairedTable(filepath.Join(collector.procRoot, "net", file))
		if err != nil {
			return samples, err
		}
		samples = append(samples, counters...)
	}

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
			if !slices.Contains(netstatFields[protocol], field) {
				continue
			}
			value, err := parseNumber(values[column+1], path)
			if err != nil {
				return nil, err
			}
			samples = append(samples, gauge("system_netstat", value, telemetry.Labels{"protocol": protocol, "field": field}))
		}
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
