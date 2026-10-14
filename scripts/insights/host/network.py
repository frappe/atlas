from host.charts import BITS_PER_SECOND, BYTES, PER_SECOND, Chart
from host.queries import field_increases_sql, fields_sql, label, netstat_increases_sql, union_sql
from workbook import COUNT, gauge_sql, rate_sql

SECTIONS = [
	(
		"Network Traffic",
		[
			Chart(
				key="net-packets",
				title="Network Traffic by Packets",
				sql=rate_sql(
					["system_network_receive_packets_total", "system_network_transmit_packets_total"],
					"concat(if(d.metric = 'system_network_receive_packets_total', 'recv ', 'trans '), d.labels['interface'])",
				),
				measure="Packets/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="net-errors",
				title="Network Traffic Errors",
				sql=rate_sql(
					["system_network_receive_errors_total", "system_network_transmit_errors_total"],
					"concat(if(d.metric = 'system_network_receive_errors_total', 'recv ', 'trans '), d.labels['interface'])",
					per_second=False,
				),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-drops",
				title="Network Traffic Drop",
				sql=rate_sql(
					["system_network_receive_drops_total", "system_network_transmit_drops_total"],
					"concat(if(d.metric = 'system_network_receive_drops_total', 'recv ', 'trans '), d.labels['interface'])",
					per_second=False,
				),
				measure="Drops",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-compressed",
				title="Network Traffic Compressed",
				sql=rate_sql(
					[
						"system_network_receive_compressed_total",
						"system_network_transmit_compressed_total",
					],
					"concat(if(d.metric = 'system_network_receive_compressed_total', 'recv ', 'trans '), d.labels['interface'])",
				),
				measure="Packets/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="net-multicast",
				title="Network Traffic Multicast",
				sql=rate_sql(["system_network_receive_multicast_total"], label("interface")),
				measure="Packets/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="net-fifo",
				title="Network Traffic Fifo",
				sql=rate_sql(
					[
						"system_network_receive_fifo_errors_total",
						"system_network_transmit_fifo_errors_total",
					],
					"concat(if(d.metric = 'system_network_receive_fifo_errors_total', 'recv ', 'trans '), d.labels['interface'])",
					per_second=False,
				),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-frame",
				title="Network Traffic Frame",
				sql=rate_sql(
					["system_network_receive_frame_errors_total"], label("interface"), per_second=False
				),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-carrier",
				title="Network Traffic Carrier",
				sql=rate_sql(
					["system_network_transmit_carrier_errors_total"], label("interface"), per_second=False
				),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-collisions",
				title="Network Traffic Colls",
				sql=rate_sql(
					["system_network_transmit_collisions_total"], label("interface"), per_second=False
				),
				measure="Collisions",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="net-conntrack",
				title="NF Conntrack",
				sql=union_sql(
					gauge_sql("system_network_conntrack_entries", "'entries'"),
					gauge_sql("system_network_conntrack_entries_limit", "'limit'"),
				),
				measure="Entries",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="net-conntrack-events",
				title="NF Conntrack Events",
				sql=field_increases_sql(
					"system_network_conntrack_stat_total",
					["drop", "early_drop", "insert_failed", "invalid", "search_restart"],
					per_second=False,
				),
				measure="Events",
				aggregation="sum",
				number_format=COUNT,
				info="Drops and insert failures mean the table is full.",
			),
			Chart(
				key="net-arp",
				title="ARP Entries",
				sql=gauge_sql("system_network_arp_entries", "s.labels['device']"),
				measure="Entries",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="net-mtu",
				title="MTU",
				sql=gauge_sql("system_network_mtu_bytes", "s.labels['interface']"),
				measure="Bytes",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="net-speed",
				title="Speed",
				sql=gauge_sql("system_network_speed_bytes", "s.labels['interface']", value="s.value * 8"),
				measure="Bits/s",
				aggregation="max",
				number_format=BITS_PER_SECOND,
			),
			Chart(
				key="net-queue",
				title="Queue Length",
				sql=gauge_sql("system_network_transmit_queue_length", "s.labels['interface']"),
				measure="Packets",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="net-softnet",
				title="Softnet Packets",
				sql=rate_sql(
					["system_network_softnet_processed_total", "system_network_softnet_dropped_total"],
					"if(d.metric = 'system_network_softnet_processed_total', 'processed', 'dropped')",
				),
				measure="Packets/s",
				aggregation="sum",
				number_format=PER_SECOND,
			),
			Chart(
				key="net-squeezed",
				title="Softnet Out of Quota",
				sql=rate_sql(
					["system_network_softnet_time_squeezed_total"],
					"concat('cpu', d.labels['cpu'])",
					per_second=False,
				),
				measure="Squeezes",
				aggregation="sum",
				number_format=COUNT,
				info="The kernel ran out of time to drain a NIC queue. Steady squeezes mean receive processing cannot keep up.",
			),
			Chart(
				key="net-status",
				title="Network Operational Status",
				sql=union_sql(
					gauge_sql("system_network_up", "concat(s.labels['interface'], ' up')"),
					gauge_sql("system_network_carrier", "concat(s.labels['interface'], ' carrier')"),
				),
				measure="Flag",
				aggregation="min",
				number_format=COUNT,
			),
			Chart(
				key="net-driver-drops",
				title="NIC Driver Drops and Errors",
				sql=rate_sql(
					["system_ethtool"],
					"concat(d.labels['interface'], ' ', d.labels['field'])",
					"AND match(labels['field'], '(?i)(drop|discard|miss|err|fail|timeout|full|no_buf)') AND NOT match(labels['field'], '(?i)^(tx|rx)_?(packets|bytes)$')",
					per_second=False,
				),
				measure="Events",
				aggregation="sum",
				number_format=COUNT,
				split_values=20,
				info="Driver counters from ethtool -S that count drops, discards, misses, and errors.",
			),
			Chart(
				key="net-missed",
				title="NIC Missed and Overrun Frames",
				sql=rate_sql(
					["system_network_rx_missed_errors_total", "system_network_rx_over_errors_total"],
					"concat(d.labels['interface'], if(d.metric = 'system_network_rx_missed_errors_total', ' missed', ' overrun'))",
					per_second=False,
				),
				measure="Frames",
				aggregation="sum",
				number_format=COUNT,
			),
		],
	),
	(
		"Network Sockstat",
		[
			Chart(
				key="sock-tcp",
				title="Sockstat TCP",
				sql=fields_sql(
					"system_sockstat",
					["alloc", "inuse", "mem", "orphan", "tw"],
					"AND s.labels['protocol'] = 'TCP'",
				),
				measure="Sockets",
				aggregation="avg",
				number_format=COUNT,
			),
			Chart(
				key="sock-udp",
				title="Sockstat UDP",
				sql=gauge_sql(
					"system_sockstat",
					"concat(s.labels['protocol'], ' ', s.labels['field'])",
					"AND s.labels['protocol'] IN ('UDP', 'UDPLITE') AND s.labels['field'] IN ('inuse', 'mem')",
				),
				measure="Sockets",
				aggregation="avg",
				number_format=COUNT,
			),
			Chart(
				key="sock-used",
				title="Sockstat Used",
				sql=fields_sql("system_sockstat", ["used"], "AND s.labels['protocol'] = 'sockets'"),
				measure="Sockets",
				aggregation="avg",
				number_format=COUNT,
			),
			Chart(
				key="sock-memory",
				title="Sockstat Memory Size",
				sql=gauge_sql(
					"system_sockstat", "s.labels['protocol']", "AND s.labels['field'] = 'mem_bytes'"
				),
				measure="Bytes",
				aggregation="avg",
				number_format=BYTES,
			),
			Chart(
				key="sock-frag",
				title="Sockstat FRAG / RAW",
				sql=gauge_sql(
					"system_sockstat",
					"concat(s.labels['protocol'], ' ', s.labels['field'])",
					"AND s.labels['protocol'] IN ('FRAG', 'RAW') AND s.labels['field'] IN ('inuse', 'memory')",
				),
				measure="Value",
				aggregation="avg",
				number_format=COUNT,
			),
		],
	),
	(
		"Network Netstat",
		[
			Chart(
				key="ns-octets",
				title="Netstat IP In / Out Octets",
				sql=netstat_increases_sql("IpExt", ["InOctets", "OutOctets"], scale="8"),
				measure="Bits/s",
				aggregation="avg",
				number_format=BITS_PER_SECOND,
			),
			Chart(
				key="ns-forwarding",
				title="Netstat IP Forwarding",
				sql=gauge_sql(
					"system_netstat",
					"'forwarding'",
					"AND s.labels['protocol'] = 'Ip' AND s.labels['field'] = 'Forwarding'",
				),
				measure="Flag",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="ns-icmp",
				title="ICMP In / Out",
				sql=netstat_increases_sql("Icmp", ["InMsgs", "OutMsgs"]),
				measure="Messages/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="ns-icmp-errors",
				title="ICMP Errors",
				sql=netstat_increases_sql("Icmp", ["InErrors"], per_second=False),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="ns-udp",
				title="UDP In / Out",
				sql=netstat_increases_sql("Udp", ["InDatagrams", "OutDatagrams"]),
				measure="Datagrams/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="ns-udp-errors",
				title="UDP Errors",
				sql=netstat_increases_sql(
					"Udp", ["InErrors", "NoPorts", "RcvbufErrors", "SndbufErrors"], per_second=False
				),
				measure="Errors",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="ns-tcp",
				title="TCP In / Out",
				sql=netstat_increases_sql("Tcp", ["InSegs", "OutSegs"]),
				measure="Segments/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="ns-tcp-errors",
				title="TCP Errors",
				sql=rate_sql(
					["system_netstat"],
					"concat(d.labels['protocol'], ' ', d.labels['field'])",
					"AND ((labels['protocol'] = 'TcpExt' AND labels['field'] IN ('ListenOverflows', 'ListenDrops', 'TCPSynRetrans')) OR (labels['protocol'] = 'Tcp' AND labels['field'] IN ('RetransSegs', 'InErrs')))",
					per_second=False,
				),
				measure="Events",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="ns-tcp-connections",
				title="TCP Connections",
				sql=gauge_sql(
					"system_netstat",
					"s.labels['field']",
					"AND s.labels['protocol'] = 'Tcp' AND s.labels['field'] IN ('CurrEstab', 'MaxConn')",
				),
				measure="Connections",
				aggregation="max",
				number_format=COUNT,
			),
			Chart(
				key="ns-syncookies",
				title="TCP SynCookie",
				sql=netstat_increases_sql(
					"TcpExt", ["SyncookiesFailed", "SyncookiesRecv", "SyncookiesSent"], per_second=False
				),
				measure="Cookies",
				aggregation="sum",
				number_format=COUNT,
			),
			Chart(
				key="ns-transitions",
				title="TCP Direct Transition",
				sql=netstat_increases_sql("Tcp", ["ActiveOpens", "PassiveOpens"]),
				measure="Opens/s",
				aggregation="avg",
				number_format=PER_SECOND,
			),
			Chart(
				key="ns-udp-queues",
				title="UDP Queues",
				sql=gauge_sql(
					"system_network_udp_queue_bytes", "concat(s.labels['ip'], ' ', s.labels['queue'])"
				),
				measure="Bytes",
				aggregation="max",
				number_format=BYTES,
			),
		],
	),
]
