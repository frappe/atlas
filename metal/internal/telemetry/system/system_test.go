package system

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

func writeFiles(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for name, content := range files {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func valueOf(t *testing.T, samples []telemetry.Sample, metric string, labels telemetry.Labels) float64 {
	t.Helper()
	for _, sample := range samples {
		if sample.Metric != metric {
			continue
		}
		matches := len(sample.Labels) == len(labels)
		for name, value := range labels {
			matches = matches && sample.Labels[name] == value
		}
		if matches {
			return sample.Value
		}
	}
	t.Fatalf("no %s %v in %d samples", metric, labels, len(samples))
	return 0
}

func hasMetricWithLabel(samples []telemetry.Sample, name, value string) bool {
	for _, sample := range samples {
		if sample.Labels[name] == value {
			return true
		}
	}
	return false
}

func TestCPUReportsEachCoreAndUsageSinceThePreviousSnapshot(t *testing.T) {
	root := writeFiles(t, map[string]string{
		"stat": "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 60 0 40 400 0 0 0 0 0 0\ncpu1 40 0 60 400 0 0 0 0 0 0\nctxt 50\n",
	})
	collector := &cpuCollector{procRoot: root, sysRoot: t.TempDir()}
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatal(err)
	}

	// 100 ticks pass: 30 user, 10 nice, 20 system, 40 idle.
	if err := os.WriteFile(filepath.Join(root, "stat"), []byte("cpu  130 10 120 840 0 0 0 0 0 0\ncpu0 90 10 60 420 0 0 0 0 0 0\ncpu1 40 0 60 420 0 0 0 0 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	if usage := valueOf(t, samples, "system_cpu_usage_percent", nil); usage != 60 {
		t.Fatalf("usage = %v, want 60", usage)
	}
	if user := valueOf(t, samples, "system_cpu_mode_percent", telemetry.Labels{"mode": "user"}); user != 40 {
		t.Fatalf("user = %v, want 40 including nice", user)
	}
	if seconds := valueOf(t, samples, "system_cpu_seconds_total", telemetry.Labels{"cpu": "0", "mode": "user"}); seconds != 0.9 {
		t.Fatalf("cpu0 user seconds = %v, want 0.9", seconds)
	}
}

func TestMemoryKeepsEveryFieldUnderTheKernelName(t *testing.T) {
	root := writeFiles(t, map[string]string{
		"meminfo": "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 600 kB\nBuffers: 50 kB\nCached: 250 kB\nActive(anon): 20 kB\nHugePages_Total: 4\n",
		"vmstat":  "pgmajfault 7\nnr_free_pages 3\n",
	})
	samples, err := memoryCollector{procRoot: root}.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if active := valueOf(t, samples, "system_memory_info", telemetry.Labels{"field": "Active_anon"}); active != 20*1024 {
		t.Fatalf("Active_anon = %v, want %v", active, 20*1024)
	}
	if pages := valueOf(t, samples, "system_memory_info", telemetry.Labels{"field": "HugePages_Total"}); pages != 4 {
		t.Fatalf("HugePages_Total = %v, want a count of 4", pages)
	}
	if used := valueOf(t, samples, "system_memory_used_bytes", nil); used != 600*1024 {
		t.Fatalf("used = %v, want %v", used, 600*1024)
	}
	if faults := valueOf(t, samples, "system_vmstat", telemetry.Labels{"field": "pgmajfault"}); faults != 7 {
		t.Fatalf("pgmajfault = %v, want 7", faults)
	}
}

func TestNetworkSkipsVirtualMachineInterfaces(t *testing.T) {
	counters := " 100 2 0 0 0 0 0 0 200 3 0 0 0 0 0 0\n"
	root := writeFiles(t, map[string]string{
		"net/dev":          "header\nheader\n  eno1:" + counters + " vh-1001:" + counters + "    lo:" + counters,
		"net/softnet_stat": "0000000a 00000001 00000002 0 0 0 0 0 0 00000000 00000000 00000000 00000000\n0000000a 00000000 00000003 0 0 0 0 0 0 00000000 00000000 00000000 00000001\n",
		"net/arp":          "IP address HW type Flags HW address Mask Device\n10.0.0.1 0x1 0x2 aa:bb:cc:dd:ee:ff * eno1\n",
	})
	sysRoot := writeFiles(t, map[string]string{
		"class/net/eno1/operstate":                   "up\n",
		"class/net/eno1/speed":                       "1000\n",
		"class/net/eno1/statistics/rx_missed_errors": "12\n",
	})

	samples, _ := networkCollector{procRoot: root, sysRoot: sysRoot}.Collect(context.Background())
	if hasMetricWithLabel(samples, "interface", "vh-1001") || hasMetricWithLabel(samples, "interface", "lo") {
		t.Fatal("a VM or loopback interface was reported")
	}
	if missed := valueOf(t, samples, "system_network_rx_missed_errors_total", telemetry.Labels{"interface": "eno1"}); missed != 12 {
		t.Fatalf("missed = %v, want 12", missed)
	}
	if speed := valueOf(t, samples, "system_network_speed_bytes", telemetry.Labels{"interface": "eno1"}); speed != 125e6 {
		t.Fatalf("speed = %v, want 125000000 bytes per second", speed)
	}
	if squeezed := valueOf(t, samples, "system_network_softnet_time_squeezed_total", telemetry.Labels{"cpu": "1"}); squeezed != 3 {
		t.Fatalf("cpu 1 time squeezed = %v, want 3", squeezed)
	}
	if entries := valueOf(t, samples, "system_network_arp_entries", telemetry.Labels{"device": "eno1"}); entries != 1 {
		t.Fatalf("arp entries = %v, want 1", entries)
	}
}

func TestNetstatKeepsEveryProtocolCounter(t *testing.T) {
	root := writeFiles(t, map[string]string{
		"net/snmp":     "Tcp: RtoAlgorithm CurrEstab RetransSegs\nTcp: 1 7 9\n",
		"net/netstat":  "TcpExt: ListenDrops\nTcpExt: 4\n",
		"net/snmp6":    "Ip6InReceives 11\nUdp6InErrors 2\n",
		"net/sockstat": "TCP: inuse 5 mem 2\n",
	})
	samples, err := netstatCollector{procRoot: root}.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if retransmits := valueOf(t, samples, "system_netstat", telemetry.Labels{"protocol": "Tcp", "field": "RetransSegs"}); retransmits != 9 {
		t.Fatalf("RetransSegs = %v, want 9", retransmits)
	}
	if errors := valueOf(t, samples, "system_netstat", telemetry.Labels{"protocol": "Udp6", "field": "InErrors"}); errors != 2 {
		t.Fatalf("Udp6 InErrors = %v, want 2", errors)
	}
	if bytes := valueOf(t, samples, "system_sockstat", telemetry.Labels{"protocol": "TCP", "field": "mem_bytes"}); bytes != float64(2*os.Getpagesize()) {
		t.Fatalf("TCP mem bytes = %v, want two pages", bytes)
	}
}

func TestEthtoolReadsQueuePrefixedDriverStatistics(t *testing.T) {
	output := "NIC statistics:\n     rx_discards: 12\n     [0]: rx_ucast_packets: 5\n"
	samples := parseEthtoolStatistics("eno1", output)
	if discards := valueOf(t, samples, "system_ethtool", telemetry.Labels{"interface": "eno1", "field": "rx_discards"}); discards != 12 {
		t.Fatalf("rx_discards = %v, want 12", discards)
	}
	if packets := valueOf(t, samples, "system_ethtool", telemetry.Labels{"interface": "eno1", "field": "[0]_rx_ucast_packets"}); packets != 5 {
		t.Fatalf("queue packets = %v, want 5", packets)
	}
}

func TestMdadmReportsADegradedArrayAndItsRecovery(t *testing.T) {
	root := writeFiles(t, map[string]string{
		"mdstat": "Personalities : [raid1]\nmd2 : active raid1 sdb4[1](F) sda4[0]\n      931388736 blocks super 1.2 [2/1] [U_]\n      [=>...]  recovery =  8.5% (1/2) finish=10min speed=1K/sec\n\nunused devices: <none>\n",
	})
	sysRoot := writeFiles(t, map[string]string{
		"block/md2/md/degraded":            "1\n",
		"block/md2/md/mismatch_cnt":        "128\n",
		"block/md2/md/dev-sdb4/errors":     "7\n",
		"block/md2/md/dev-sdb4/state":      "faulty\n",
		"block/md2/md/dev-sdb4/bad_blocks": "2048 8\n4096 8\n",
	})
	samples, err := mdadmCollector{procRoot: root, sysRoot: sysRoot}.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if failed := valueOf(t, samples, "system_md_disks", telemetry.Labels{"device": "md2", "state": "failed"}); failed != 1 {
		t.Fatalf("failed disks = %v, want 1", failed)
	}
	if running := valueOf(t, samples, "system_md_disks_running", telemetry.Labels{"device": "md2"}); running != 1 {
		t.Fatalf("running disks = %v, want 1 of 2", running)
	}
	if progress := valueOf(t, samples, "system_md_sync_percent", telemetry.Labels{"device": "md2", "action": "recovery"}); progress != 8.5 {
		t.Fatalf("recovery = %v, want 8.5", progress)
	}
	if mismatch := valueOf(t, samples, "system_md_mismatch_cnt", telemetry.Labels{"device": "md2"}); mismatch != 128 {
		t.Fatalf("mismatch = %v, want 128", mismatch)
	}
	member := telemetry.Labels{"device": "md2", "member": "sdb4"}
	if corrected := valueOf(t, samples, "system_md_member_corrected_errors", member); corrected != 7 {
		t.Fatalf("corrected errors = %v, want 7", corrected)
	}
	if ranges := valueOf(t, samples, "system_md_member_bad_block_ranges", member); ranges != 2 {
		t.Fatalf("bad block ranges = %v, want 2", ranges)
	}
}

func TestSMARTLogReadsWearAndMediaErrors(t *testing.T) {
	log := make([]byte, nvmeSMARTLogBytes)
	log[0] = 0x04
	log[1], log[2] = 0x2c, 0x01 // 300 K
	log[5] = 17
	log[160] = 3
	samples := parseSMARTLog("nvme0", log)

	if warning := valueOf(t, samples, "system_nvme_smart", telemetry.Labels{"device": "nvme0", "field": "critical_warning"}); warning != 4 {
		t.Fatalf("critical warning = %v, want 4", warning)
	}
	if used := valueOf(t, samples, "system_nvme_smart", telemetry.Labels{"device": "nvme0", "field": "percentage_used"}); used != 17 {
		t.Fatalf("percentage used = %v, want 17", used)
	}
	if errors := valueOf(t, samples, "system_nvme_smart", telemetry.Labels{"device": "nvme0", "field": "media_errors"}); errors != 3 {
		t.Fatalf("media errors = %v, want 3", errors)
	}
}

func TestDiskSkipsVirtualMachineVolumesAndPartitions(t *testing.T) {
	counters := " 10 0 8 5 20 0 16 7 0 9 11 0 0 0 0\n"
	root := writeFiles(t, map[string]string{
		"diskstats": "   8 0 sda" + counters + "   8 1 sda1" + counters + " 230 0 zd0" + counters,
	})
	sysRoot := writeFiles(t, map[string]string{
		"block/sda/device/state":     "running\n",
		"block/sda/device/iotmo_cnt": "0x1a\n",
		"block/zd0/size":             "1\n",
	})

	samples, err := diskCollector{procRoot: root, sysRoot: sysRoot}.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if hasMetricWithLabel(samples, "device", "sda1") || hasMetricWithLabel(samples, "device", "zd0") {
		t.Fatal("a partition or VM volume was reported")
	}
	if read := valueOf(t, samples, "system_disk_read_bytes_total", telemetry.Labels{"device": "sda"}); read != 8*sectorBytes {
		t.Fatalf("read bytes = %v, want %v", read, 8*sectorBytes)
	}
	if timeouts := valueOf(t, samples, "system_disk_scsi_timeouts_total", telemetry.Labels{"device": "sda"}); timeouts != 26 {
		t.Fatalf("SCSI timeouts = %v, want 26 from hex", timeouts)
	}
}

func TestZFSReportsPoolSpaceHealthAndWorstDeviceErrors(t *testing.T) {
	status := `  pool: metal
 state: DEGRADED
config:

	NAME        STATE     READ WRITE CKSUM
	metal       DEGRADED     0     0     0
	  mirror-0  DEGRADED     0     0     0
	    sda     ONLINE       0     0     0
	    sdb     FAULTED      3     0    17

errors: No known data errors
`
	output := func(_ context.Context, _ string, args ...string) (string, error) {
		if args[0] == "list" {
			return "1000\t400\t600\t5\t40\tDEGRADED\n", nil
		}
		return status, nil
	}
	samples, err := zfsCollector{pool: "metal", procRoot: t.TempDir(), output: output}.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	if healthy := valueOf(t, samples, "system_zfs_pool_healthy", telemetry.Labels{"pool": "metal"}); healthy != 0 {
		t.Fatalf("healthy = %v, want 0 for a degraded pool", healthy)
	}
	if free := valueOf(t, samples, "system_zfs_pool_free_bytes", telemetry.Labels{"pool": "metal"}); free != 600 {
		t.Fatalf("free = %v, want 600", free)
	}
	if checksum := valueOf(t, samples, "system_zfs_device_errors_maximum", telemetry.Labels{"pool": "metal", "kind": "checksum"}); checksum != 17 {
		t.Fatalf("checksum errors = %v, want 17", checksum)
	}
}
