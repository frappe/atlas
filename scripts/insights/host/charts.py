from dataclasses import dataclass


@dataclass(frozen=True)
class Chart:
	"""One line chart in a host section. measure names the value of one line per series,
	or lists colored columns, one line each. A number format with a scale divides in SQL."""

	key: str
	title: str
	sql: str
	measure: str | list[tuple[str, str, str]]
	aggregation: str
	number_format: dict
	stack: bool = False
	split_values: int = 12
	info: str = ""


# Insights shortens 1e9 to "B" (billion), so byte values are scaled in SQL by
# "scale" and the unit goes in the suffix.
BYTES = {"decimals": 2, "suffix": " GiB", "scale": 1 << 30}
MEBIBYTES = {"decimals": 1, "suffix": " MiB", "scale": 1 << 20}
BYTES_PER_SECOND = {"decimals": 2, "suffix": " MiB/s", "scale": 1 << 20}
BITS_PER_SECOND = {"decimals": 2, "suffix": " Mb/s", "scale": 1e6}
PRECISE_SECONDS = {"decimals": 4, "suffix": " s"}
PER_SECOND = {"shorten": True, "decimals": 1, "suffix": "/s"}

# Column, series name, and color of each line in a colored chart.
CPU_COLUMNS = [
	("busy_system", "Busy System", "#dc2626"),
	("busy_user", "Busy User", "#2563eb"),
	("busy_iowait", "Busy Iowait", "#f59e0b"),
	("busy_irqs", "Busy IRQs", "#7c3aed"),
	("busy_other", "Busy Other", "#0d9488"),
]
CPU_DETAIL_COLUMNS = [
	("busy_system", "Busy System", "#dc2626"),
	("busy_user", "Busy User", "#2563eb"),
	("busy_nice", "Busy Nice", "#60a5fa"),
	("busy_iowait", "Busy Iowait", "#f59e0b"),
	("busy_irq", "Busy IRQ", "#7c3aed"),
	("busy_softirq", "Busy SoftIRQ", "#a78bfa"),
	("busy_steal", "Busy Steal", "#be185d"),
]
MEMORY_BASIC_COLUMNS = [
	("ram_used", "RAM Used", "#2563eb"),
	("cache_buffer", "RAM Cache + Buffer", "#0d9488"),
	("ram_free", "RAM Free", "#e5e7eb"),
	("swap_used", "SWAP Used", "#dc2626"),
]
MEMORY_STACK_COLUMNS = [
	("apps", "Apps", "#2563eb"),
	("page_tables", "PageTables", "#7c3aed"),
	("swap_cache", "SwapCache", "#be185d"),
	("slab", "Slab", "#f59e0b"),
	("cache", "Cache", "#0d9488"),
	("buffers", "Buffers", "#14b8a6"),
	("unused", "Unused", "#e5e7eb"),
	("swap", "Swap", "#dc2626"),
	("hardware_corrupted", "Hardware Corrupted", "#000000"),
]
VM_STATE_COLUMNS = [
	("running", "Running", "#16a34a"),
	("sleeping", "Sleeping", "#60a5fa"),
	("stopped", "Stopped", "#9ca3af"),
	("paused", "Paused", "#7c3aed"),
	("created", "Created", "#f59e0b"),
	("failed", "Failed", "#dc2626"),
]
