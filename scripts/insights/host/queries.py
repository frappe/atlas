from workbook import HOST, LOOKBACK, TITLES, gauge_sql, rate_sql

# Insights orders series by name, so "Busy ..." stacks below "Idle" as in Grafana.
BASIC_CPU_MODE = """multiIf(s.labels['mode'] = 'idle', 'Idle', s.labels['mode'] = 'user', 'Busy User',
  s.labels['mode'] = 'system', 'Busy System', s.labels['mode'] = 'iowait', 'Busy Iowait',
  s.labels['mode'] = 'irq', 'Busy IRQs', 'Busy Other')"""
CPU_MODE = """if(d.labels['mode'] = 'idle', 'Idle', concat('Busy ', multiIf(d.labels['mode'] = 'irq', 'IRQ',
  d.labels['mode'] = 'softirq', 'SoftIRQ', initcap(d.labels['mode']))))"""
VM_STATE = "initcap(s.labels['state'])"

FILESYSTEMS = f"""WITH {TITLES}
SELECT s.ts AS ts, {HOST.format(alias="s")} AS host, s.labels['mountpoint'] AS series,
  1 - sumIf(s.value, s.metric = 'system_filesystem_available_bytes') / nullIf(sumIf(s.value, s.metric = 'system_filesystem_size_bytes'), 0) AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric IN ('system_filesystem_available_bytes', 'system_filesystem_size_bytes') AND s.ts > now() - {LOOKBACK}
GROUP BY ts, host, series"""


def fields_sql(metric: str, names: list[str], where: str = "") -> str:
	"""One row per sample of a field-labelled metric, one series per field."""
	quoted = ", ".join(f"'{name}'" for name in names)
	return gauge_sql(metric, "s.labels['field']", f"AND s.labels['field'] IN ({quoted}) {where}")


def field_increases_sql(
	metric: str,
	names: list[str],
	series: str = "d.labels['field']",
	per_second: bool = True,
	scale: str = "1",
) -> str:
	"""The increase of named fields of one counter metric."""
	quoted = ", ".join(f"'{name}'" for name in names)
	return rate_sql(
		[metric], series, f"AND labels['field'] IN ({quoted})", scale=scale, per_second=per_second
	)


def derived_series_sql(
	metric: str, names: dict[str, str], outputs: list[tuple[str, str]], where: str = ""
) -> str:
	"""Pivot metric fields and report derived series at each sample time."""
	columns = ",\n    ".join(
		f"sumIf(value, labels['field'] = '{field}') AS {alias}" for alias, field in names.items()
	)
	pairs = ", ".join(f"('{name}', toFloat64({expression}))" for name, expression in outputs)
	quoted = ", ".join(f"'{field}'" for field in names.values())
	return f"""WITH {TITLES},
pivot AS (
  SELECT ts, resource_id,
    {columns}
  FROM samples
  WHERE metric = '{metric}' AND labels['field'] IN ({quoted}) AND ts > now() - {LOOKBACK} {where}
  GROUP BY ts, resource_id
)
SELECT p.ts AS ts, {HOST.format(alias="p")} AS host, pair.1 AS series, pair.2 AS value
FROM pivot AS p LEFT JOIN titles AS t ON t.server = p.resource_id
ARRAY JOIN [{pairs}] AS pair"""


def counter_ratio_sql(numerator: str, denominator: str, series: str = "labels['device']") -> str:
	"""Divide counter increases per interval, such as read time per completed read."""
	return f"""WITH {TITLES},
deltas AS (
  SELECT ts, resource_id, metric, {series} AS series,
    greatest(value - lagInFrame(value, 1, value) OVER w, 0) AS increase
  FROM samples
  WHERE metric IN ('{numerator}', '{denominator}') AND ts > now() - {LOOKBACK}
  WINDOW w AS (PARTITION BY resource_id, metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT d.ts AS ts, {HOST.format(alias="d")} AS host, d.series AS series,
  sumIf(d.increase, d.metric = '{numerator}') / nullIf(sumIf(d.increase, d.metric = '{denominator}'), 0) AS value
FROM deltas AS d LEFT JOIN titles AS t ON t.server = d.resource_id
GROUP BY ts, host, series"""


def label(name: str) -> str:
	"""The value of one label of a rate_sql row."""
	return f"d.labels['{name}']"


def union_sql(*queries: str) -> str:
	"""One query with the rows of all queries."""
	return " UNION ALL ".join(queries)


def netstat_increases_sql(protocol: str, names: list[str], per_second: bool = True, scale: str = "1") -> str:
	"""The increase of named netstat counters of one protocol."""
	quoted = ", ".join(f"'{name}'" for name in names)
	return rate_sql(
		["system_netstat"],
		"d.labels['field']",
		f"AND labels['protocol'] = '{protocol}' AND labels['field'] IN ({quoted})",
		scale=scale,
		per_second=per_second,
	)


def filesystem_used_sql() -> str:
	"""The bytes used on each mounted file system."""
	return f"""WITH {TITLES}
SELECT s.ts AS ts, {HOST.format(alias="s")} AS host, s.labels['mountpoint'] AS series,
  sumIf(s.value, s.metric = 'system_filesystem_size_bytes') - sumIf(s.value, s.metric = 'system_filesystem_available_bytes') AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric IN ('system_filesystem_size_bytes', 'system_filesystem_available_bytes') AND s.ts > now() - {LOOKBACK}
GROUP BY ts, host, series"""
