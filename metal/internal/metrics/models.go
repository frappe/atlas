// Package metrics collects and retains VM measurements on disk.
package metrics

// Sample describes the current resource use of one virtual machine.
// Embedded groups keep the stored JSON fields flat.
type Sample struct {
	Up bool `json:"up"`
	ComputeUsage
	DiskUsage
	NetworkUsage
}

// ComputeUsage holds cumulative CPU time and current memory use.
type ComputeUsage struct {
	CPUTimeMicroseconds uint64 `json:"cpu_usage_microseconds"`
	MemoryBytes         uint64 `json:"memory_bytes"`
}

// DiskUsage holds disk size, configured limits, and measured I/O rates.
type DiskUsage struct {
	DiskMiB                  int          `json:"disk_mib"`
	DiskUsedMiB              int          `json:"disk_used_mib"`
	DiskThroughputLimitMiBps int          `json:"disk_throughput_limit_mibps"`
	DiskIOPSLimit            int          `json:"disk_iops_limit"`
	DiskReadBytesPerSecond   uint64       `json:"disk_read_bytes_per_second"`
	DiskWriteBytesPerSecond  uint64       `json:"disk_write_bytes_per_second"`
	DiskReadMilliIOPS        uint64       `json:"disk_read_milli_iops"`
	DiskWriteMilliIOPS       uint64       `json:"disk_write_milli_iops"`
	Counters                 DiskCounters `json:"-"`
}

// DiskCounters are cumulative root disk I/O counters from the VM cgroup.
type DiskCounters struct {
	ReadBytes       uint64
	WriteBytes      uint64
	ReadOperations  uint64
	WriteOperations uint64
}

// NetworkUsage holds cumulative traffic counters for both IP families.
type NetworkUsage struct {
	ReceivedBytes     uint64 `json:"received_bytes"`
	ReceivedPackets   uint64 `json:"received_packets"`
	SentBytes         uint64 `json:"sent_bytes"`
	SentPackets       uint64 `json:"sent_packets"`
	SentICMPPackets   uint64 `json:"sent_icmp_packets"`
	SentUDPPackets    uint64 `json:"sent_udp_packets"`
	SentTCPSYNPackets uint64 `json:"sent_tcp_syn_packets"`
	SentTCPRSTPackets uint64 `json:"sent_tcp_rst_packets"`
}
