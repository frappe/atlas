from host.charts import BYTES, BYTES_PER_SECOND, PER_SECOND, PRECISE_SECONDS, Chart
from host.queries import counter_ratio_sql, field_increases_sql, fields_sql, label, union_sql
from workbook import COUNT, PERCENT, gauge_sql, rate_sql

SECTIONS = [
	(
		"Storage Disk",
		[
			Chart(
				key="disk-ops",
				title="Disk IOps Completed",
				sql=rate_sql(
					["system_disk_reads_completed_total", "system_disk_writes_completed_total"],
					"concat(d.labels['device'], if(d.metric = 'system_disk_reads_completed_total', ' read', ' write'))",
				),
				measure="IO/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="disk-data",
				title="Disk R/W Data",
				sql=rate_sql(
					["system_disk_read_bytes_total", "system_disk_written_bytes_total"],
					"concat(d.labels['device'], if(d.metric = 'system_disk_read_bytes_total', ' read', ' write'))",
				),
				measure="Bytes/s",
				aggregation="avg",
				number_format=BYTES_PER_SECOND,
			),
			Chart(
				key="disk-read-wait",
				title="Disk Average Read Wait Time",
				sql=counter_ratio_sql(
					"system_disk_read_time_seconds_total", "system_disk_reads_completed_total"
				),
				measure="Seconds",
				aggregation="avg",
				number_format=PRECISE_SECONDS,
			),
			Chart(
				key="disk-write-wait",
				title="Disk Average Write Wait Time",
				sql=counter_ratio_sql(
					"system_disk_write_time_seconds_total", "system_disk_writes_completed_total"
				),
				measure="Seconds",
				aggregation="avg",
				number_format=PRECISE_SECONDS,
			),
			Chart(
				key="disk-queue",
				title="Average Queue Size",
				sql=rate_sql(["system_disk_io_weighted_time_seconds_total"], label("device")),
				measure="Queue",
				aggregation="avg",
				number_format={"decimals": 2},
			),
			Chart(
				key="disk-merged",
				title="Disk R/W Merged",
				sql=rate_sql(
					["system_disk_reads_merged_total", "system_disk_writes_merged_total"],
					"concat(d.labels['device'], if(d.metric = 'system_disk_reads_merged_total', ' read', ' write'))",
				),
				measure="Merged/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="disk-time",
				title="Time Spent Doing I/Os",
				sql=rate_sql(
					["system_disk_io_time_seconds_total", "system_disk_discard_time_seconds_total"],
					"concat(d.labels['device'], if(d.metric = 'system_disk_io_time_seconds_total', ' io', ' discard'))",
					scale="100",
				),
				measure="%",
				aggregation="avg",
				number_format=PERCENT,
			),
			Chart(
				key="disk-now",
				title="Instantaneous Queue Size",
				sql=gauge_sql("system_disk_io_in_progress", "s.labels['device']"),
				measure="Requests",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="disk-discards",
				title="Disk IOps Discards Completed / Merged",
				sql=rate_sql(
					["system_disk_discards_completed_total", "system_disk_discards_merged_total"],
					"concat(d.labels['device'], if(d.metric = 'system_disk_discards_completed_total', ' completed', ' merged'))",
				),
				measure="Per second",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="disk-raid",
				title="RAID Disks",
				sql=gauge_sql("system_md_disks", "concat(s.labels['device'], ' ', s.labels['state'])"),
				measure="Disks",
				aggregation="max",
				number_format=COUNT,
				info="Software RAID member disks by state. A failed disk or a running count below the required count means a degraded array.",
			),
		],
	),
	(
		"Storage Filesystem",
		[
			Chart(
				key="fs-available",
				title="Filesystem Space Available",
				sql=gauge_sql("system_filesystem_available_bytes", "s.labels['mountpoint']"),
				measure="Bytes",
				aggregation="min",
				number_format=BYTES,
			),
			Chart(
				key="fs-inodes-free",
				title="File Nodes Free",
				sql=gauge_sql("system_filesystem_inodes_free", "s.labels['mountpoint']"),
				measure="Inodes",
				aggregation="min",
				number_format=COUNT,
			),
			Chart(
				key="fs-descriptors",
				title="File Descriptor",
				sql=union_sql(
					gauge_sql("system_open_files", "'allocated'"),
					gauge_sql("system_open_files_limit", "'maximum'"),
				),
				measure="Files",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="fs-inodes",
				title="File Nodes Size",
				sql=gauge_sql("system_filesystem_inodes", "s.labels['mountpoint']"),
				measure="Inodes",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="fs-readonly",
				title="Filesystem in ReadOnly / Error",
				sql=union_sql(
					gauge_sql("system_filesystem_read_only", "concat(s.labels['mountpoint'], ' read only')"),
					gauge_sql("system_filesystem_device_error", "concat(s.labels['mountpoint'], ' error')"),
				),
				measure="Flag",
				aggregation="max",
				number_format=COUNT,
			),
		],
	),
	(
		"Disk Health",
		[
			Chart(
				key="smart-temperature",
				title="Disk Temperature",
				sql=union_sql(
					gauge_sql("system_smart_temperature_celsius", "s.labels['device']"),
					gauge_sql(
						"system_nvme_smart",
						"s.labels['device']",
						"AND s.labels['field'] = 'temperature_celsius'",
					),
				),
				measure="°C",
				aggregation="max",
				number_format={"decimals": 0, "suffix": " °C"},
			),
			Chart(
				key="smart-sectors",
				title="Bad Sectors",
				sql=gauge_sql(
					"system_smart_attribute",
					"concat(s.labels['device'], ' ', s.labels['attribute'])",
					"AND s.labels['field'] = 'raw' AND s.labels['id'] IN ('5', '187', '197', '198', '199')",
				),
				measure="Count",
				aggregation="max",
				number_format=COUNT,
				info="Reallocated, uncorrectable, pending, and offline uncorrectable sectors, and SATA CRC errors. A rising count is a failing disk or cable.",
			),
			Chart(
				key="scsi-timeouts",
				title="Disk Command Timeouts",
				sql=rate_sql(["system_disk_scsi_timeouts_total"], label("device"), per_second=False),
				measure="Timeouts",
				aggregation="sum",
				number_format=COUNT,
				info="Commands to a SATA or SAS disk that timed out. Timeouts point to a failing disk, cable, or controller.",
			),
			Chart(
				key="raid-health",
				title="RAID Mismatches and Corrected Errors",
				sql=union_sql(
					gauge_sql("system_md_mismatch_cnt", "concat(s.labels['device'], ' mismatched sectors')"),
					gauge_sql("system_md_degraded", "concat(s.labels['device'], ' missing members')"),
					gauge_sql(
						"system_md_member_corrected_errors",
						"concat(s.labels['device'], ' ', s.labels['member'], ' corrected')",
					),
				),
				measure="Count",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="zfs-damage",
				title="ZFS Data Errors and Slow I/Os",
				sql=union_sql(
					gauge_sql("system_zfs_pool_data_errors", "concat(s.labels['pool'], ' data errors')"),
					gauge_sql("system_zfs_device_slow_ios", "concat(s.labels['device'], ' slow')"),
				),
				measure="Count",
				aggregation="max",
				number_format=COUNT,
				info="Data errors are blocks that no copy could repair. Slow I/Os took longer than 30 seconds.",
			),
			Chart(
				key="zfs-scrub-age",
				title="Days Since Last Scrub",
				sql=gauge_sql(
					"system_zfs_scan_end_timestamp_seconds",
					"concat(s.labels['pool'], ' ', s.labels['function'])",
					value="(toUnixTimestamp(s.ts) - s.value) / 86400",
				),
				measure="Days",
				aggregation="max",
				number_format={"decimals": 1, "suffix": " days"},
				info="Scrubs run every month. More than 35 days means a scrub did not run.",
			),
		],
	),
	(
		"ZFS",
		[
			Chart(
				key="zfs-pool",
				title="Pool Capacity and Fragmentation",
				sql=union_sql(
					gauge_sql("system_zfs_pool_capacity_percent", "concat(s.labels['pool'], ' used')"),
					gauge_sql(
						"system_zfs_pool_fragmentation_percent",
						"concat(s.labels['pool'], ' fragmentation')",
					),
				),
				measure="%",
				aggregation="max",
				number_format=PERCENT,
			),
			Chart(
				key="zfs-arc-size",
				title="ARC Size",
				sql=fields_sql(
					"system_zfs_kstat",
					["size", "c", "c_max", "c_min"],
					"AND s.labels['kstat'] = 'arcstats'",
				),
				measure="Bytes",
				aggregation="avg",
				number_format=BYTES,
			),
			Chart(
				key="zfs-arc-hits",
				title="ARC Hits and Misses",
				sql=rate_sql(
					["system_zfs_kstat"],
					label("field"),
					"AND labels['kstat'] = 'arcstats' AND labels['field'] IN ('hits', 'misses', 'demand_data_misses', 'prefetch_data_misses')",
				),
				measure="Per second",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="zfs-pool-io",
				title="Pool I/O",
				sql=rate_sql(
					["system_zfs_pool_kstat"],
					label("field"),
					"AND labels['kstat'] = 'iostats' AND match(labels['field'], 'bytes$')",
				),
				measure="Bytes/s",
				aggregation="avg",
				number_format=BYTES_PER_SECOND,
			),
			Chart(
				key="zfs-zil",
				title="ZIL Commits",
				sql=rate_sql(
					["system_zfs_kstat"],
					label("field"),
					"AND labels['kstat'] = 'zil' AND labels['field'] IN ('zil_commit_count', 'zil_commit_writer_count', 'zil_itx_count')",
				),
				measure="Per second",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="zfs-errors",
				title="Device Errors",
				sql=gauge_sql(
					"system_zfs_device_errors", "concat(s.labels['device'], ' ', s.labels['kind'])"
				),
				measure="Errors",
				aggregation="max",
				number_format=COUNT,
				info="Read, write, and checksum errors of each pool device. Any value above zero needs an operator.",
			),
			Chart(
				key="zfs-throttle",
				title="ARC Memory Throttles",
				sql=field_increases_sql("system_zfs_kstat", ["memory_throttle_count"], per_second=False),
				measure="Throttles",
				aggregation="sum",
				number_format=COUNT,
			),
		],
	),
]
