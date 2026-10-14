from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import frappe
import requests
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.server_providers.generic import RedfishClient, RedfishError
from atlas.atlas.core.telemetry.datum import get_datum_url, issue_datum_token, send_log_lines, send_samples

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

# Limit each host's reads so a slow BMC cannot consume the whole job timeout.
HOSTS_PER_JOB = 5
HOST_TIMEOUT_SECONDS = 90
JOB_TIMEOUT_SECONDS = 600
LOG_LOOKBACK = timedelta(days=30)

HEALTH = {"OK": 0, "Warning": 1, "Critical": 2}
SEEN_ENTRIES_KEY = "atlas:telemetry:bmc_seen_entries:{server}"


def enqueue_bmc_telemetry() -> None:
	"""Queue BMC telemetry jobs in groups of hosts."""
	if (
		not get_datum_url()
		or not frappe.get_cached_doc("Atlas Settings").server_provider_controller.has_bmc_driver
	):
		return

	servers = frappe.get_all(
		"Metal Server",
		filters={"bmc_url": ["is", "set"], "status": ["!=", "Deleted"]},
		order_by="name asc",
		pluck="name",
	)
	for start in range(0, len(servers), HOSTS_PER_JOB):
		group = servers[start : start + HOSTS_PER_JOB]
		frappe.enqueue(
			"atlas.atlas.core.telemetry.bmc.publish_bmc_telemetry",
			queue="long",
			timeout=JOB_TIMEOUT_SECONDS,
			job_id=f"atlas||bmc-telemetry||{','.join(group)}",
			deduplicate=True,
			servers=group,
		)


def publish_bmc_telemetry(servers: list[str]) -> None:
	"""Publish BMC metrics and new events for each host."""
	url = get_datum_url()
	for name in servers:
		# Inventory changes can put the same host in two queued groups.
		try:
			with filelock(f"atlas:telemetry:bmc:{name}", timeout=0):
				BMCCollector(frappe.get_doc("Metal Server", name)).publish(url)
		except LockTimeoutError:
			continue
		except requests.RequestException as error:
			frappe.logger("atlas.telemetry").warning(f"Datum BMC push failed for {name}: {error}")
		except Exception:
			frappe.log_error(title=f"BMC telemetry failed for Metal Server {name}")


class BMCCollector:
	"""Collect and publish one host's Redfish metrics and events."""

	def __init__(self, server: MetalServer) -> None:
		self.server = server
		provider = server.settings.server_provider_controller
		self.client = provider.get_bmc_client(server, deadline=time.monotonic() + HOST_TIMEOUT_SECONDS)
		self.now = datetime.now(UTC)
		self.clock_offset = timedelta()
		self.samples: list[dict] = []
		self.lines: list[dict] = []
		self.seen_entries_key = SEEN_ENTRIES_KEY.format(server=server.name)
		self.seen_entries: dict[str, list[str]] = frappe.cache.get_value(self.seen_entries_key) or {}
		self.current_entries = dict(self.seen_entries)

	def publish(self, url: str) -> None:
		"""Publish telemetry before recording events as sent."""
		self.collect()
		token = issue_datum_token(self.server.name)
		send_samples(url, token, self.samples)
		if self.lines:
			send_log_lines(url, token, self.lines)
		frappe.cache.set_value(self.seen_entries_key, self.current_entries)

	def collect(self) -> None:
		"""Collect system, chassis, manager, and log data."""
		try:
			system = self.client.read(self.client.url)
		except RedfishError as error:
			self.add_sample("bmc_up", 0)
			self._log_read_error(error)
			return

		self.add_sample("bmc_up", 1)

		links = system.get("Links") or {}
		chassis = self.read_linked_resource(links.get("Chassis"))
		manager = self.read_linked_resource(links.get("ManagedBy"))
		self.add_system(system, manager)
		self.add_health("chassis", chassis)
		self.add_health("manager", manager)
		for add_section, resource in (
			(self.add_components, system),
			(self.add_thermal, chassis),
			(self.add_power, chassis),
			(self.add_logs, system),
			(self.add_logs, chassis),
			(self.add_logs, manager),
		):
			try:
				add_section(resource)
			except RedfishError as error:
				self._log_read_error(error)

	def read_linked_resource(self, value: dict | list | None) -> dict:
		"""Read a linked resource, or return an empty dict on absence or failure."""
		link = RedfishClient.get_link(value)
		try:
			return self.client.read(link) if link else {}
		except RedfishError as error:
			self._log_read_error(error)
			return {}

	def add_system(self, system: dict, manager: dict) -> None:
		"""Add system details, clock offset, and system health."""
		processors = system.get("ProcessorSummary") or {}
		memory = system.get("MemorySummary") or {}
		self.add_sample(
			"bmc_system_info",
			1,
			manufacturer=system.get("Manufacturer"),
			model=system.get("Model"),
			serial=system.get("SerialNumber"),
			bios_version=system.get("BiosVersion"),
			bmc_firmware=manager.get("FirmwareVersion"),
			processor_model=" ".join(str(processors.get("Model") or "").split()),
			processor_count=processors.get("Count"),
			memory_gib=memory.get("TotalSystemMemoryGiB"),
			power_state=system.get("PowerState"),
		)
		self.add_health("system", system)

		clock = self.parse_time(manager.get("DateTime"))
		if clock:
			self.clock_offset = timedelta(seconds=round((clock - self.now).total_seconds()))
			self.add_sample("bmc_clock_offset_seconds", self.clock_offset.total_seconds())

	def add_components(self, system: dict) -> None:
		"""Add memory and processor health."""
		for kind, collection in (("memory", "Memory"), ("processor", "Processors")):
			link = RedfishClient.get_link(system.get(collection))
			for member in self.client.read_members(link) if link else []:
				self.add_component(kind, self.client.read(member["@odata.id"]))

	def add_thermal(self, chassis: dict) -> None:
		"""Add present temperature sensors and fans."""
		link = RedfishClient.get_link(chassis.get("Thermal"))
		thermal = self.client.read(link) if link else {}
		for sensor in thermal.get("Temperatures") or []:
			if RedfishClient.is_present(sensor):
				name = RedfishClient.get_name(sensor)
				self.add_sample("bmc_temperature_celsius", sensor.get("ReadingCelsius"), name=name)
				self.add_sample(
					"bmc_temperature_critical_celsius", sensor.get("UpperThresholdCritical"), name=name
				)

		for fan in thermal.get("Fans") or []:
			if RedfishClient.is_present(fan):
				self.add_component("fan", fan)
				name = RedfishClient.get_name(fan)
				self.add_sample("bmc_fan_speed", fan.get("Reading"), name=name, unit=fan.get("ReadingUnits"))

	def add_power(self, chassis: dict) -> None:
		"""Add power use, present power supplies, and voltage readings."""
		link = RedfishClient.get_link(chassis.get("Power"))
		power = self.client.read(link) if link else {}
		for control in power.get("PowerControl") or []:
			name = RedfishClient.get_name(control)
			metrics = control.get("PowerMetrics") or {}
			self.add_sample("bmc_power_watts", control.get("PowerConsumedWatts"), name=name, kind="consumed")
			self.add_sample("bmc_power_watts", metrics.get("AverageConsumedWatts"), name=name, kind="average")
			self.add_sample("bmc_power_watts", metrics.get("MaxConsumedWatts"), name=name, kind="maximum")

		for supply in power.get("PowerSupplies") or []:
			if RedfishClient.is_present(supply):
				name = RedfishClient.get_name(supply)
				self.add_component("power_supply", supply)
				self.add_sample("bmc_power_supply_input_volts", supply.get("LineInputVoltage"), name=name)
				self.add_sample(
					"bmc_power_supply_output_watts", supply.get("LastPowerOutputWatts"), name=name
				)

		for rail in power.get("Voltages") or []:
			if RedfishClient.is_present(rail):
				self.add_sample(
					"bmc_voltage_volts", rail.get("ReadingVolts"), name=RedfishClient.get_name(rail)
				)

	def add_logs(self, resource: dict) -> None:
		"""Collect new Warning and Critical entries from a resource's logs."""
		for link in self.client.read_log_links(resource):
			try:
				log, entries = self.client.read_log(link)
			except RedfishError as error:
				self._log_read_error(error)
				continue
			self.add_new_log_entries(link, log, entries)

	def add_new_log_entries(self, link: str, log: str, entries: list[dict]) -> None:
		"""Add new or updated events by ID and timestamp."""
		is_first_read = link not in self.seen_entries
		seen_keys = set(self.seen_entries.get(link, []))
		keys = [f"{entry.get('Id')}|{entry.get('Updated') or entry.get('Created')}" for entry in entries]
		for entry, key in zip(entries, keys, strict=True):
			if key in seen_keys or entry.get("Severity") not in ("Warning", "Critical"):
				continue

			# BMCs can copy events from another log.
			origin = RedfishClient.get_link((entry.get("Links") or {}).get("OriginOfCondition")) or ""
			if "/LogServices/" in origin:
				continue

			created = self.parse_time(entry.get("Created"))
			created = created - self.clock_offset if created else None
			if is_first_read and (not created or created < self.now - LOG_LOOKBACK):
				continue
			self.lines.append(self.to_log_line(log, entry, created))
		self.current_entries[link] = keys

	def to_log_line(self, log: str, entry: dict, created: datetime | None) -> dict:
		"""Build a log line with the corrected event time or read time."""
		earliest = self.now - LOG_LOOKBACK
		is_created_valid = created and earliest < created < self.now + timedelta(minutes=5)
		attributes = {
			"log": log,
			"entry_id": entry.get("Id"),
			"message_id": entry.get("MessageId"),
			"sensor_type": entry.get("SensorType"),
			"entry_type": entry.get("EntryType"),
			"created": entry.get("Created"),
		}
		return {
			"ts": (created if is_created_valid else self.now).isoformat(),
			"product": "metal",
			"source": "bmc",
			"service": "bmc",
			"level": entry["Severity"].lower(),
			"message": " ".join(str(entry.get("Message") or entry.get("Name") or "").split()),
			"attributes": {name: str(value) for name, value in attributes.items() if value},
		}

	def add_component(self, kind: str, component: dict) -> None:
		"""Add a present component's health."""
		health = (component.get("Status") or {}).get("Health")
		if RedfishClient.is_present(component) and health in HEALTH:
			self.add_sample(
				"bmc_component_health", HEALTH[health], kind=kind, name=RedfishClient.get_name(component)
			)

	def add_health(self, component: str, resource: dict) -> None:
		"""Add aggregate health, falling back to resource health."""
		status = resource.get("Status") or {}
		health = status.get("HealthRollup") or status.get("Health")
		if health in HEALTH:
			self.add_sample("bmc_health", HEALTH[health], component=component)

	def add_sample(self, metric: str, value: Any, **labels: Any) -> None:
		"""Add a sample when its value is numeric."""
		if isinstance(value, int | float):
			self.samples.append(
				{
					"metric": metric,
					"value": float(value),
					"ts": self.now.isoformat(),
					"labels": {
						name: str(label).strip() for name, label in labels.items() if label is not None
					},
				}
			)

	@staticmethod
	def parse_time(value: str | None) -> datetime | None:
		"""Parse a timestamp as UTC, assuming UTC when its offset is missing."""
		try:
			parsed = datetime.fromisoformat(value or "")
		except ValueError:
			return None
		return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

	def _log_read_error(self, error: RedfishError) -> None:
		frappe.logger("atlas.telemetry").warning(f"BMC read failed for {self.server.name}: {error}")
