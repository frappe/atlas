from host.dashboard import DEFAULT_HOST
from logs_dashboard import LINES, log_table
from workbook import (
	HOST,
	LOOKBACK,
	TITLES,
	Layout,
	Workbook,
	dimension,
	gauge_sql,
	measure,
	numbers,
	table,
	time_series,
)

# All samples of one BMC read share the bmc_up timestamp.
LATEST_READ = f"""{TITLES},
reads AS (
  SELECT resource_id, max(ts) AS read_ts FROM samples
  WHERE metric = 'bmc_up' AND ts > now() - INTERVAL 1 DAY
  GROUP BY resource_id
),
latest AS (
  SELECT s.resource_id AS resource_id, s.metric AS metric, s.labels AS labels, s.value AS value, p.read_ts AS read_ts
  FROM samples AS s INNER JOIN reads AS p ON p.resource_id = s.resource_id AND p.read_ts = s.ts
  WHERE s.metric LIKE 'bmc_%' AND s.ts > now() - INTERVAL 1 DAY
),
temperatures AS (
  SELECT resource_id, labels['name'] AS sensor, maxIf(value, metric = 'bmc_temperature_celsius') AS reading,
    maxIfOrNull(value, metric = 'bmc_temperature_critical_celsius') AS critical, critical - reading AS headroom
  FROM latest
  WHERE metric IN ('bmc_temperature_celsius', 'bmc_temperature_critical_celsius')
  GROUP BY resource_id, sensor
  HAVING countIf(metric = 'bmc_temperature_celsius') > 0
)"""

HEALTH_STATUS = "multiIf({value} IS NULL, 'Unknown', {value} >= 2, 'Critical', {value} >= 1, 'Warning', 'OK')"

# Include configured BMCs without recent data so missing reads remain visible.
BMC_HOSTS = f"""WITH {LATEST_READ},
listed AS (
  SELECT labels['server'] AS resource_id FROM samples
  WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY resource_id
  HAVING argMax(labels['bmc'], ts) = 'true' AND argMax(labels['status'], ts) != 'Deleted'
),
readings AS (
  SELECT resource_id, any(read_ts) AS read_ts, maxIf(value, metric = 'bmc_up') AS reachable,
    countIf(metric IN ('bmc_health', 'bmc_component_health') AND value > 0) AS unhealthy_components
  FROM latest
  GROUP BY resource_id
),
events AS (
  SELECT resource_id, countIf(level = 'critical') AS critical_events, countIf(level = 'warning') AS warning_events
  FROM logs
  WHERE source = 'bmc' AND ts > now() - INTERVAL 1 DAY
  GROUP BY resource_id
),
headroom AS (
  SELECT resource_id, min(headroom) AS temperature_headroom FROM temperatures GROUP BY resource_id
),
hosts AS (
  SELECT resource_id FROM listed
  UNION DISTINCT
  SELECT resource_id FROM reads WHERE read_ts > now() - INTERVAL 15 MINUTE
)
SELECT {HOST.format(alias="h")} AS host, ifNull(r.reachable, 0) AS reachable,
  if(r.read_ts IS NULL, 1440, dateDiff('minute', r.read_ts, now())) AS minutes_since_read,
  ifNull(r.unhealthy_components, 0) AS unhealthy_components, ifNull(e.critical_events, 0) AS critical_events,
  ifNull(e.warning_events, 0) AS warning_events,
  m.temperature_headroom AS temperature_headroom,
  toUInt8(reachable = 0) + toUInt8(minutes_since_read >= 15) + toUInt8(unhealthy_components > 0) + toUInt8(critical_events > 0)
    AS problems
FROM hosts AS h LEFT JOIN titles AS t ON t.server = h.resource_id
LEFT JOIN readings AS r ON r.resource_id = h.resource_id
LEFT JOIN events AS e ON e.resource_id = h.resource_id
LEFT JOIN headroom AS m ON m.resource_id = h.resource_id"""

BMC_READINGS = [
	("Last Read", "minutes_since_read", "max", {"decimals": 0, "suffix": " min ago"}),
	("Unhealthy Components", "unhealthy_components", "max", {"decimals": 0}),
	("Critical Events, last day", "critical_events", "max", {"decimals": 0}),
	("Temperature Headroom", "temperature_headroom", "min", {"decimals": 0, "suffix": " °C"}),
]

SYSTEM = f"""WITH {LATEST_READ},
info AS (
  SELECT resource_id, any(labels) AS labels FROM latest WHERE metric = 'bmc_system_info' GROUP BY resource_id
),
clock AS (
  SELECT resource_id, round(abs(any(value)) / 60) AS clock_offset_minutes FROM latest
  WHERE metric = 'bmc_clock_offset_seconds'
  GROUP BY resource_id
)
SELECT {HOST.format(alias="i")} AS host, i.labels['manufacturer'] AS manufacturer, i.labels['model'] AS model,
  i.labels['serial'] AS serial, i.labels['bios_version'] AS bios, i.labels['bmc_firmware'] AS bmc_firmware,
  i.labels['processor_model'] AS processor, i.labels['memory_gib'] AS memory_gib, i.labels['power_state'] AS power,
  c.clock_offset_minutes AS clock_offset_minutes
FROM info AS i LEFT JOIN titles AS t ON t.server = i.resource_id
LEFT JOIN clock AS c ON c.resource_id = i.resource_id"""

COMPONENTS = f"""WITH {LATEST_READ}
SELECT {HOST.format(alias="l")} AS host, if(l.metric = 'bmc_health', 'summary', l.labels['kind']) AS kind,
  if(l.metric = 'bmc_health', l.labels['component'], l.labels['name']) AS name,
  {HEALTH_STATUS.format(value="l.value")} AS status, l.value AS health
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.metric IN ('bmc_health', 'bmc_component_health')"""

TEMPERATURES = f"""WITH {LATEST_READ}
SELECT {HOST.format(alias="m")} AS host, m.sensor AS sensor, m.reading AS reading, m.critical AS critical,
  m.headroom AS headroom
FROM temperatures AS m LEFT JOIN titles AS t ON t.server = m.resource_id"""

POWER_SUPPLIES = f"""WITH {LATEST_READ}
SELECT {HOST.format(alias="l")} AS host, l.labels['name'] AS power_supply,
  {HEALTH_STATUS.format(value="maxIfOrNull(l.value, l.metric = 'bmc_component_health')")} AS status,
  maxIfOrNull(l.value, l.metric = 'bmc_power_supply_input_volts') AS input_volts,
  maxIfOrNull(l.value, l.metric = 'bmc_power_supply_output_watts') AS output_watts
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.metric IN ('bmc_power_supply_input_volts', 'bmc_power_supply_output_watts')
  OR (l.metric = 'bmc_component_health' AND l.labels['kind'] = 'power_supply')
GROUP BY host, power_supply"""

VOLTAGES = f"""WITH {LATEST_READ}
SELECT {HOST.format(alias="l")} AS host, l.labels['name'] AS rail, l.value AS volts
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.metric = 'bmc_voltage_volts'"""

EVENT_COLUMNS = [("critical", "Critical", "#dc2626"), ("warning", "Warning", "#f59e0b")]
EVENTS_PER_HOUR = f"""WITH {TITLES}
SELECT toStartOfHour(l.ts) AS ts, {HOST.format(alias="l")} AS host,
  countIf(l.level = 'critical') AS critical, countIf(l.level = 'warning') AS warning
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.source = 'bmc' AND l.ts > now() - {LOOKBACK}
GROUP BY ts, host"""

EVENTS = f"""WITH {TITLES}
SELECT l.ts AS ts, {HOST.format(alias="l")} AS host, l.level AS level, l.attributes['log'] AS log,
  l.attributes['sensor_type'] AS sensor_type, l.message AS message
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.source = 'bmc' AND l.ts > now() - {LOOKBACK}"""

STATUS_COLORS = {"Critical": "red", "Warning": "amber", "OK": "green"}


def add_bmc_dashboard(workbook: Workbook) -> None:
	"""Build the host BMC dashboard."""
	folder = workbook.folder("Host BMC")

	availability = folder.add_chart(
		"bmc-availability",
		"BMC Availability",
		gauge_sql("bmc_up", value="s.value * 100"),
		"Number",
		{
			**numbers([measure("BMC Availability", "value", "avg")]),
			"number_formats": {"BMC Availability": {"decimals": 1, "suffix": "%"}},
		},
		"Percentage of successful system reads during the selected Period.",
	)
	now = folder.add_chart(
		"bmc-now",
		"BMC Now",
		BMC_HOSTS,
		"Number",
		{
			**numbers([measure(name, column, aggregation) for name, column, aggregation, _ in BMC_READINGS]),
			"number_formats": {name: number_format for name, _, _, number_format in BMC_READINGS},
		},
		"Unhealthy Components counts Warning and Critical health reports, including summaries. "
		"Temperature Headroom is the smallest gap to a critical threshold.",
	)
	system = folder.add_chart(
		"bmc-system",
		"System",
		SYSTEM,
		"Table",
		table(
			[
				dimension(column)
				for column in (
					"host",
					"manufacturer",
					"model",
					"serial",
					"bios",
					"bmc_firmware",
					"processor",
					"memory_gib",
					"power",
				)
			],
			[measure("Clock Offset min", "clock_offset_minutes", "max")],
		),
		"Clock Offset is the absolute difference between the BMC and Atlas clocks, used to adjust event times.",
	)
	components = folder.add_chart(
		"bmc-components",
		"Components",
		COMPONENTS,
		"Table",
		table(
			[dimension("host"), dimension("kind"), dimension("name"), dimension("status")],
			[measure("Health", "health", "max", "Integer")],
			order="Health",
			status_colors=STATUS_COLORS,
		),
		"Component health, worst first. Summary rows include aggregate system, chassis, and BMC health when available.",
	)
	temperatures = folder.add_chart(
		"bmc-temperatures",
		"Temperatures",
		gauge_sql("bmc_temperature_celsius", "s.labels['name']"),
		"Line",
		time_series(measure("°C", "value", "max"), split_values=20),
	)
	temperatures_now = folder.add_chart(
		"bmc-temperatures-now",
		"Temperature Headroom",
		TEMPERATURES,
		"Table",
		table(
			[dimension("host"), dimension("sensor")],
			[
				measure("Reading °C", "reading", "max"),
				measure("Critical °C", "critical", "max"),
				measure("Headroom °C", "headroom", "min"),
			],
			order="Headroom °C",
			direction="asc",
		),
		"Headroom is the critical threshold minus the reading, smallest first. Empty values mean no threshold.",
	)
	fans = folder.add_chart(
		"bmc-fans",
		"Fan Speed",
		gauge_sql("bmc_fan_speed", "concat(s.labels['name'], ' ', s.labels['unit'])"),
		"Line",
		time_series(measure("Speed", "value", "max"), split_values=20),
	)
	power = folder.add_chart(
		"bmc-power",
		"Power",
		gauge_sql("bmc_power_watts", "s.labels['name']", "AND s.labels['kind'] = 'consumed'"),
		"Line",
		time_series(measure("Watts", "value", "max"), number_format={"decimals": 0, "suffix": " W"}),
	)
	power_supplies = folder.add_chart(
		"bmc-power-supplies",
		"Power Supplies",
		POWER_SUPPLIES,
		"Table",
		table(
			[dimension("host"), dimension("power_supply"), dimension("status")],
			[measure("Input V", "input_volts", "max"), measure("Output W", "output_watts", "max")],
			order="power_supply",
			direction="asc",
			status_colors=STATUS_COLORS,
		),
	)
	voltages = folder.add_chart(
		"bmc-voltages",
		"Voltage Rails",
		VOLTAGES,
		"Table",
		table(
			[dimension("host"), dimension("rail")],
			[measure("Volts", "volts", "max")],
			order="rail",
			direction="asc",
		),
	)
	event_volume = folder.add_chart(
		"bmc-event-volume",
		"Events per Hour",
		EVENTS_PER_HOUR,
		"Bar",
		{
			"x_axis": {"dimension": dimension("ts", "Datetime", granularity="hour")},
			"y_axis": {
				"series": [
					{"measure": measure(name, column, "sum", "Integer"), "color": [color], "type": "bar"}
					for column, name, color in EVENT_COLUMNS
				],
				"stack": True,
			},
			"limit": 10000,
		},
	)
	events = folder.add_chart(
		"bmc-events",
		"Event Log",
		EVENTS,
		"Table",
		log_table(
			["host", "level", "log", "sensor_type", "message"],
			[LINES],
			status_colors={"critical": "red", "warning": "amber"},
			status_column="level",
		),
		"New or updated Warning and Critical events. Copies from other BMC logs are excluded.",
	)
	timed = [availability, temperatures, fans, power, event_volume, events]

	layout = Layout(workbook, "Atlas - Host BMC")
	layout.filter(
		"Period",
		"Date",
		workbook.filter_links(timed, "ts"),
		default_operator="within",
		default_value={"span": "last 1 day (include current)"},
	)
	layout.filter(
		"Host",
		"String",
		workbook.filter_links(
			[now, system, components, temperatures_now, power_supplies, voltages, *timed], "host"
		),
		default_operator="=",
		default_value=DEFAULT_HOST or None,
	)
	layout.heading("Now")
	layout.reading(availability, "BMC Availability", width=4)
	layout.readings(now, [name for name, _, _, _ in BMC_READINGS], width=4)
	layout.chart(system, 20, 9)
	layout.heading("Components and Events")
	layout.chart(components)
	layout.chart(event_volume)
	layout.chart(events, 20, 24)
	layout.heading("Thermal")
	layout.chart(temperatures_now)
	layout.chart(temperatures)
	layout.chart(fans, 20, 14)
	layout.heading("Power")
	layout.chart(power_supplies, 10, 12)
	layout.chart(power, 10, 12)
	layout.chart(voltages, 20, 18)
