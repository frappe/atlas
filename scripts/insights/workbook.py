DATA_SOURCE = "datum"
LOOKBACK = "INTERVAL 14 DAY"

PERCENT = {"decimals": 1, "suffix": "%"}
UPTIME_PERCENT = {"decimals": 2, "suffix": "%"}
COUNT = {"shorten": True, "decimals": 0}
SECONDS = {"decimals": 1, "suffix": " s"}

TITLES = f"""titles AS (
  SELECT labels['server'] AS server, argMax(labels['title'], ts) AS title
  FROM samples
  WHERE metric = 'atlas_metal_server_info' AND ts > now() - {LOOKBACK}
  GROUP BY server
)"""

# A disk is unhealthy when it fails its own check, has reallocated or pending
# sectors, or is an NVMe disk with a critical warning or media errors.
UNHEALTHY_DISKS = """disks AS (
  SELECT resource_id, countIf(unhealthy) AS unhealthy_disks
  FROM (
    SELECT resource_id, labels['device'] AS device,
      coalesce(minIfOrNull(value, metric = 'system_smart_healthy'), 1) = 0
        OR maxIf(value, metric = 'system_smart_attribute' AND labels['field'] = 'raw' AND labels['id'] IN ('5', '197')) > 0
        OR maxIf(value, metric = 'system_nvme_smart' AND labels['field'] IN ('critical_warning', 'media_errors')) > 0 AS unhealthy
    FROM samples
    WHERE ts > now() - INTERVAL 1 HOUR AND metric IN ('system_smart_healthy', 'system_smart_attribute', 'system_nvme_smart')
    GROUP BY resource_id, device
  )
  GROUP BY resource_id
)"""

HOST = "if(t.title = '', {alias}.resource_id, t.title)"


def gauge_sql(metric: str, series: str = "''", where: str = "", value: str = "s.value") -> str:
	"""One row per sample of a gauge."""
	return f"""WITH {TITLES}
SELECT s.ts AS ts, {HOST.format(alias="s")} AS host, {series} AS series, {value} AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric = '{metric}' AND s.ts > now() - {LOOKBACK} {where}"""


def rate_sql(
	metrics: list[str], series: str = "''", where: str = "", scale: str = "1", per_second: bool = True
) -> str:
	"""Return counter increases or rates, with zero for reset intervals."""
	names = ", ".join(f"'{metric}'" for metric in metrics)
	return f"""WITH {TITLES},
deltas AS (
  SELECT ts, resource_id, metric, labels,
    value - lagInFrame(value, 1, value) OVER w AS increase,
    dateDiff('millisecond', lagInFrame(ts, 1, ts) OVER w, ts) / 1000 AS seconds
  FROM samples
  WHERE metric IN ({names}) AND ts > now() - {LOOKBACK} {where}
  WINDOW w AS (PARTITION BY resource_id, metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
SELECT d.ts AS ts, {HOST.format(alias="d")} AS host, {series} AS series,
  if(d.seconds > 0 AND d.increase >= 0, {"d.increase / d.seconds * " + scale if per_second else "d.increase"}, 0) AS value
FROM deltas AS d LEFT JOIN titles AS t ON t.server = d.resource_id"""


# Each chart reads a fixed window, so a dashboard Period filter does not apply.
WEEK = "today() - 6"

ERRORS = "source = 'error_log' AND service = 'atlas'"
# Lines at level error or worse from a Metal host: metald, the kernel, or a systemd unit.
HOST_ERRORS = "resource_id NOT LIKE 'atlas-%' AND level IN ('error', 'critical', 'alert', 'emergency')"


def daily_increase_sql(metrics: list[str], series: str, where: str = "") -> str:
	"""One row per day and series: how much a set of counters grew that day."""
	names = ", ".join(f"'{metric}'" for metric in metrics)
	return f"""SELECT toDate(ts) AS day, {series} AS series, sum(greatest(increase, 0)) AS value
FROM (
  SELECT ts, metric, labels, value - lagInFrame(value, 1, value) OVER w AS increase
  FROM samples
  WHERE metric IN ({names}) AND ts >= {WEEK} - 1 {where}
  WINDOW w AS (PARTITION BY resource_id, metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE ts >= {WEEK}
GROUP BY day, series"""


class Workbook:
	"""Build Insights queries, charts, and dashboards from Datum SQL."""

	def __init__(self, title: str) -> None:
		self.title = title
		self.queries: dict = {}
		self.charts: dict = {}
		self.dashboards: dict = {}
		self.folders: list = []

	def query(self, key: str, title: str, sql: str, folder: str) -> str:
		"""Add a native query and return its name."""
		self.add_folder(folder, "query")
		self.queries[key] = {
			"name": key,
			"title": title,
			"workbook": "wb-atlas",
			"folder": folder,
			"sort_order": len(self.queries),
			"use_live_connection": 1,
			"is_script_query": 0,
			"is_builder_query": 0,
			"is_native_query": 1,
			"operations": [{"type": "sql", "raw_sql": sql, "data_source": DATA_SOURCE}],
		}
		return key

	def chart(
		self, key: str, title: str, query: str, chart_type: str, config: dict, folder: str, info: str = ""
	) -> str:
		"""Add a chart of query and return its name."""
		self.add_folder(folder, "chart")
		config.setdefault("filters", {"logical_operator": "And", "filters": []})
		self.charts[key] = {
			"name": key,
			"title": title,
			"description": None,
			"info": info or None,
			"workbook": "wb-atlas",
			"folder": folder,
			"sort_order": len(self.charts),
			"query": query,
			"chart_type": chart_type,
			"config": config,
		}
		return key

	def folder(self, title: str) -> "Folder":
		"""Return the folder that a dashboard adds its queries and charts to."""
		return Folder(self, title)

	def filter_links(self, charts: list[str], column: str) -> dict:
		"""Route one dashboard filter to the same column of each chart's query."""
		return {chart: f"`{self.charts[chart]['query']}`.`{column}`" for chart in charts}

	def add_folder(self, title: str, kind: str) -> None:
		"""Add a query or chart folder once."""
		if not any(folder["title"] == title and folder["type"] == kind for folder in self.folders):
			self.folders.append({"title": title, "type": kind, "sort_order": len(self.folders)})

	def export(self) -> dict:
		"""Return the workbook in the Insights export format."""
		return {
			"doctype": "Insights Workbook",
			"name": "wb-atlas",
			"title": self.title,
			"folders": self.folders,
			"queries": self.queries,
			"charts": self.charts,
			"dashboards": self.dashboards,
		}


class Folder:
	"""One workbook folder. Each chart in it has its own query of the same key."""

	def __init__(self, workbook: Workbook, title: str) -> None:
		self.workbook = workbook
		self.title = title

	def add_chart(self, key: str, title: str, sql: str, chart_type: str, config: dict, info: str = "") -> str:
		"""Add the query `q-<key>` and its chart `c-<key>`, and return the chart name."""
		query = self.workbook.query(f"q-{key}", title, sql, self.title)
		return self.workbook.chart(f"c-{key}", title, query, chart_type, config, self.title, info)


def measure(name: str, column: str, aggregation: str, data_type: str = "Decimal", **extra) -> dict:
	"""A measure that aggregates one query column."""
	return {
		"measure_name": name,
		"column_name": column,
		"data_type": data_type,
		"aggregation": aggregation,
		**extra,
	}


def dimension(column: str, data_type: str = "String", name: str | None = None, **extra) -> dict:
	"""A dimension that groups by one query column."""
	return {"dimension_name": name or column, "column_name": column, "data_type": data_type, **extra}


def line_style(granularity: str, stack: bool = False) -> dict:
	"""Fill stacked series and smooth coarse charts; dense samples can produce loops."""
	style = {"show_area": stack}
	if granularity not in ("second", "minute"):
		style.update({"smooth": True, "show_data_points": True, "show_data_labels": True})
	return style


def time_series(
	value: dict,
	granularity: str = "minute",
	split: bool = True,
	split_values: int = 12,
	number_format: dict | None = None,
	**y_axis,
) -> dict:
	"""A line chart of value over `ts`, with one line per `series` value when split."""
	config = {
		"x_axis": {"dimension": dimension("ts", "Datetime", granularity=granularity)},
		"y_axis": {
			"series": [{"measure": value}],
			**line_style(granularity, y_axis.get("stack", False)),
			**y_axis,
		},
		"limit": 10000,
	}
	if number_format:
		config["number_formats"] = {value["measure_name"]: number_format}
	if split:
		config["split_by"] = {"dimension": dimension("series"), "max_split_values": split_values}
	return config


def table(
	rows: list[dict],
	values: list[dict],
	order: str | None = None,
	direction: str = "desc",
	limit: int = 100,
	scale: str | None = None,
	status_colors: dict[str, str] | None = None,
) -> dict:
	"""Build a sorted table with optional value and status colors."""
	config = {"rows": rows, "columns": [], "values": values, "limit": limit, "show_row_totals": False}
	if order:
		config["order_by"] = [{"column": {"type": "column", "column_name": order}, "direction": direction}]
	formats = [
		{
			"mode": "text_rules",
			"column": {"type": "column", "column_name": "status"},
			"color": color,
			"operator": "equals_text",
			"value": text,
		}
		for text, color in (status_colors or {}).items()
	]
	if scale:
		formats.append(
			{
				"mode": "color_scale",
				"column": {"type": "column", "column_name": scale},
				"colorScale": "ascending",
				"scaleScope": "local",
			}
		)
	if formats:
		config["conditional_formatting"] = {"formats": formats, "columns": []}
	return config


def numbers(
	readings: list[dict],
	colors: list[str] | None = None,
	trend_grain: str | None = None,
	is_latest: bool = True,
) -> dict:
	"""Build number cards with optional trends and latest-bucket values."""
	config = {
		"number_columns": readings,
		"number_column_options": [{"color": color} for color in colors] if colors else [{} for _ in readings],
		"number_formats": {reading["measure_name"]: {"decimals": 0, "shorten": True} for reading in readings},
	}
	if trend_grain:
		config["date_column"] = dimension("ts", "Datetime", granularity=trend_grain)
		config["sparkline"] = True
	if trend_grain and is_latest:
		config["window"] = {"grain": trend_grain}
	return config


class Layout:
	"""Place dashboard items left to right in the 20-column grid, row by row."""

	def __init__(self, workbook: Workbook, key: str, title: str) -> None:
		self.items: list = []
		self.x = 0
		self.y = 0
		self.row_height = 0
		workbook.dashboards[key] = {
			"name": key,
			"title": title,
			"route": title.lower().replace(" - ", "-").replace(" ", "-"),
			"workbook": "wb-atlas",
			"vertical_compact_layout": 1,
			"items": self.items,
		}

	def place(self, item: dict, width: int, height: int) -> None:
		"""Place item after the previous one, or on a new row when it does not fit."""
		if self.x + width > 20:
			self.new_row()
		item["layout"] = {"i": f"item-{len(self.items)}", "x": self.x, "y": self.y, "w": width, "h": height}
		self.items.append(item)
		self.x += width
		self.row_height = max(self.row_height, height)

	def new_row(self) -> None:
		"""Start the next item on a new row."""
		if self.x:
			self.y += self.row_height
		self.x, self.row_height = 0, 0

	def heading(self, text: str) -> None:
		"""Add a full-width section heading."""
		self.new_row()
		self.place({"type": "text", "text": f"<h3>{text}</h3>"}, 20, 2)
		self.new_row()

	def chart(self, chart: str, width: int = 10, height: int = 18) -> None:
		"""Place one chart."""
		self.place({"type": "chart", "chart": chart}, width, height)

	def reading(self, chart: str, name: str, width: int = 5, height: int = 7) -> None:
		"""Place the number card of one reading of a Number chart."""
		self.place({"type": "chart", "chart": chart, "reading": name}, width, height)

	def readings(self, chart: str, names: list[str], width: int = 5, height: int = 7) -> None:
		"""Place one number card for each reading, then start a new row."""
		for name in names:
			self.reading(chart, name, width, height)
		self.new_row()

	def filter(self, name: str, filter_type: str, links: dict, **defaults) -> None:
		"""Add a dashboard filter that applies to the linked chart columns."""
		icon = {"Date": "calendar", "String": "type"}.get(filter_type, "hash")
		self.place(
			{
				"type": "filter",
				"filter_name": name,
				"filter_type": filter_type,
				"icon": icon,
				"links": links,
				**defaults,
			},
			4,
			2,
		)


def trend(value_name: str, number_format: dict, stack: bool = True, granularity: str = "day") -> dict:
	"""Build daily or weekly series with optional stacks."""
	return {
		"x_axis": {"dimension": dimension("day", "Date", granularity=granularity)},
		"y_axis": {
			"series": [{"measure": measure(value_name, "value", "sum")}],
			"stack": stack,
			**line_style("day", stack),
		},
		"split_by": {"dimension": dimension("series"), "max_split_values": 10},
		"number_formats": {value_name: number_format},
		"limit": 10000,
	}
