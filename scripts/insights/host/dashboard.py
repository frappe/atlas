import os

from host.charts import Chart
from host.compute import SECTIONS as COMPUTE
from host.memory import SECTIONS as MEMORY
from host.network import SECTIONS as NETWORK
from host.storage import SECTIONS as STORAGE
from host.summary import (
	DISKS_NOW,
	HOST_QUICK,
	HOST_TREND,
	POOLS_NOW,
	QUICK_READINGS,
	RECONCILERS_NOW,
	TREND_READINGS,
	VM_READINGS,
	VMS_NOW,
)
from host.system import SECTIONS as SYSTEM
from workbook import (
	PERCENT,
	Folder,
	Layout,
	Workbook,
	dimension,
	line_style,
	measure,
	numbers,
	table,
	time_series,
)

# The Host filter shows one host. ATLAS_INSIGHTS_DEFAULT_HOST names the one that opens first.
DEFAULT_HOST = os.environ.get("ATLAS_INSIGHTS_DEFAULT_HOST", "")


def wide_sql(sql: str, series_column: str, columns: list[tuple[str, str, str]]) -> str:
	"""Pivot series into columns for separate chart colors."""
	pivots = ",\n  ".join(
		f"avgIf(value, {series_column} = '{name}') AS {column}" for column, name, _ in columns
	)
	return f"""SELECT ts, host,
  {pivots}
FROM ({sql})
GROUP BY ts, host"""


def colored_series(
	columns: list[tuple[str, str, str]], aggregation: str, number_format: dict, **y_axis
) -> dict:
	"""A time series with one measure per column, each in its own color."""
	series = [
		{"measure": measure(name, column, aggregation), "color": [color]} for column, name, color in columns
	]
	return {
		"x_axis": {"dimension": dimension("ts", "Datetime", granularity="minute")},
		"y_axis": {"series": series, **line_style("minute", y_axis.get("stack", False)), **y_axis},
		"number_format": number_format,
		"limit": 10000,
	}


def add_section_chart(folder: Folder, chart: Chart) -> str:
	"""Add one section chart, with one colored line per column when it lists columns."""
	options = {"stack": True} if chart.stack else {}
	if chart.number_format is PERCENT:
		options.update({"min": 0, "max": 100})
	number_format = dict(chart.number_format)
	scale = number_format.pop("scale", None)

	if isinstance(chart.measure, list):
		sql = wide_sql(chart.sql, "series", chart.measure)
		if scale:
			scaled = ", ".join(f"{column} / {scale} AS {column}" for column, _, _ in chart.measure)
			sql = f"SELECT ts, host, {scaled} FROM ({sql})"
		config = colored_series(chart.measure, chart.aggregation, number_format, **options)
	else:
		sql = chart.sql
		if scale:
			sql = f"SELECT ts, host, series, value / {scale} AS value FROM ({sql})"
		value = measure(chart.measure, "value", chart.aggregation)
		config = time_series(value, split_values=chart.split_values, number_format=number_format, **options)
	return folder.add_chart(chart.key, chart.title, sql, "Line", config, chart.info)


def add_host_dashboard(workbook: Workbook) -> None:
	"""Node Exporter Full sections for the host that the Host filter picks."""
	folder = workbook.folder("Host")

	trend = folder.add_chart(
		"host-trend",
		"Now",
		HOST_TREND,
		"Number",
		{
			**numbers(
				[measure(name, column, "avg") for name, column in TREND_READINGS],
				trend_grain="minute",
			),
			"number_formats": {
				name: {"decimals": 0} if name == "Running VMs" else PERCENT for name, _ in TREND_READINGS
			},
		},
		"The newest minute of each reading, with its trend over the period. Load is the load average per CPU core. "
		"Disk Busy is the busiest disk's share of time spent on I/O.",
	)
	quick = folder.add_chart(
		"host-quick",
		"Quick CPU / Mem / Disk",
		HOST_QUICK,
		"Number",
		{
			**numbers(
				[
					measure("CPU Cores", "cores", "max", "Integer"),
					measure("RAM Total", "ram_total", "max"),
					measure("Pool Size", "pool_size", "max"),
					measure("Failed Units", "failed_units", "max", "Integer"),
					measure("Unhealthy Disks", "unhealthy_disks", "max", "Integer"),
					measure("Uptime", "uptime_days", "min"),
					measure("CPU Temp", "cpu_temperature", "max"),
					measure("Memory Temp", "memory_temperature", "max"),
				]
			),
			"number_formats": {
				"CPU Cores": {"decimals": 0},
				"RAM Total": {"decimals": 1, "suffix": " GiB"},
				"Pool Size": {"decimals": 0, "suffix": " GiB"},
				"Failed Units": {"decimals": 0},
				"Unhealthy Disks": {"decimals": 0},
				"Uptime": {"decimals": 1, "suffix": " days"},
				"CPU Temp": {"decimals": 1, "suffix": " °C"},
				"Memory Temp": {"decimals": 1, "suffix": " °C"},
			},
		},
	)
	vms = folder.add_chart(
		"vms-now",
		"VMs",
		VMS_NOW,
		"Number",
		numbers([measure(name, column, "max", "Integer") for name, column in VM_READINGS]),
	)
	reconcilers = folder.add_chart(
		"reconcilers-now",
		"Reconcile Loops",
		RECONCILERS_NOW,
		"Table",
		table(
			[dimension("host"), dimension("loop")],
			[measure("Since Last Pass s", "since_last_pass_seconds", "max", "Integer")],
			order="Since Last Pass s",
			scale="Since Last Pass s",
		),
		"A VM loop passes every 5 seconds. A growing value means the loop is stuck.",
	)
	disks = folder.add_chart(
		"disks-now",
		"Disks",
		DISKS_NOW,
		"Table",
		table(
			[dimension("device"), dimension("model")],
			[
				measure("Healthy", "healthy", "min", "Integer"),
				measure("SMART Status", "exit_status", "max", "Integer"),
				measure("Temp °C", "temperature", "max", "Integer"),
				measure("Power On Days", "power_on_days", "max", "Integer"),
				measure("Life Used %", "life_used", "max", "Integer"),
				measure("Reallocated", "reallocated", "max", "Integer"),
				measure("Pending", "pending", "max", "Integer"),
				measure("Uncorrectable", "uncorrectable", "max", "Integer"),
				measure("I/O Timeouts", "io_timeouts", "max", "Integer"),
			],
			order="device",
			direction="asc",
		),
		"Healthy 0 means the disk fails its own check. SMART Status is the smartctl exit bitmask: 8 failing, "
		"16 attribute at threshold, 64 error log, 128 failed self-test.",
	)
	pools = folder.add_chart(
		"pools-now",
		"Pools and Arrays",
		POOLS_NOW,
		"Table",
		table(
			[dimension("name"), dimension("state"), dimension("scan")],
			[
				measure("Data Errors", "data_errors", "max", "Integer"),
				measure("Device Errors", "device_errors", "max", "Integer"),
				measure("Days Since Scrub", "scrub_age_days", "max"),
				measure("Scrub Errors", "scrub_errors", "max", "Integer"),
				measure("Repaired MiB", "repaired_mib", "max"),
			],
			order="name",
			direction="asc",
		),
		"Data Errors are ZFS blocks that no copy could repair, or md sectors that differ between copies. "
		"Device Errors is the worst device or member count.",
	)
	sections = [
		(heading, [add_section_chart(folder, chart) for chart in charts])
		for heading, charts in [*COMPUTE, *MEMORY, *SYSTEM, *STORAGE, *NETWORK]
	]
	section_charts = [chart for _, charts in sections for chart in charts]

	layout = Layout(workbook, "Atlas - Host")
	layout.filter(
		"Period",
		"Date",
		workbook.filter_links(section_charts, "ts"),
		default_operator="within",
		default_value={"span": "last 1 day (include current)"},
	)
	layout.filter(
		"Host",
		"String",
		workbook.filter_links([*section_charts, reconcilers, quick, trend, vms, disks, pools], "host"),
		default_operator="=",
		default_value=DEFAULT_HOST or None,
	)
	layout.heading("Quick CPU / Mem / Disk")
	layout.readings(trend, [name for name, _ in TREND_READINGS])
	layout.readings(quick, QUICK_READINGS, height=4)
	for heading, charts in sections:
		layout.heading(heading)
		if heading == "Virtual Machines":
			layout.readings(vms, [name for name, _ in VM_READINGS], height=4)
		if heading == "Disk Health":
			layout.chart(disks, 20, 12)
			layout.chart(pools, 20, 13)
		for chart in charts:
			layout.chart(chart)
		if heading == "metald":
			layout.chart(reconcilers)
