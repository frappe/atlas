from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import ceil
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.server_providers.base import ACCEPTED_OS_VERSIONS
from atlas.atlas.core.ssh import SSHResult, SSHRunner

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

REPORT_START = "===INSPECTION_START==="
REPORT_END = "===INSPECTION_END==="
OS_NAMES = {"ubuntu": "Ubuntu", "debian": "Debian"}
# Metal architecture names for the machine types that Atlas publishes binaries for.
SUPPORTED_MACHINES = {"x86_64": "amd64"}
# WireGuard adds 60 bytes to each packet and needs 1280 bytes for IPv6.
MINIMUM_PRIVATE_NETWORK_MTU = 1340
MIB = 1024**2
GIB = 1024**3
# ZFS uses this file as the pool device on a host without a free disk.
DISK_IMAGE_PATH = "/root/disks/atlas.img"
DISK_IMAGE_DEFAULT_PERCENT = 80


class HostReportError(Exception):
	"""Report a host probe that Atlas cannot use."""


@dataclass(frozen=True, slots=True)
class InspectedDisk:
	"""Store one whole disk and the reason it cannot hold the storage pool."""

	device: str
	size_gib: int
	busy_reason: str


@dataclass(frozen=True, slots=True)
class HostReport:
	"""Store the host facts that the probe prints and check them."""

	hostname: str
	machine: str
	os: str
	os_version: str
	cpu_count: int
	memory_mib: int
	has_kvm: bool
	public_network_interface: str
	private_network_interface: str
	private_network_mtu: int
	disks: tuple[InspectedDisk, ...]
	image_available_gib: int

	@classmethod
	def parse(cls, output: str, public_ipv4_address: str, private_ipv4_address: str) -> "HostReport":
		"""Parse the JSON report between the probe markers."""
		_, has_start, rest = output.partition(REPORT_START)
		body, has_end, _ = rest.partition(REPORT_END)
		if not has_start or not has_end:
			raise HostReportError("The host probe printed no report")

		try:
			report = json.loads(body)
			interface_name, interface_mtu = cls._find_interface(report["addresses"], private_ipv4_address)
			public_interface_name = cls._find_interface(report["addresses"], public_ipv4_address)[0]
			default_route_interfaces = [
				str(route["dev"]) for route in report["default_routes"] if route.get("dev")
			]
			return cls(
				hostname=str(report["hostname"]),
				machine=str(report["machine"]),
				os=OS_NAMES.get(report["os_id"], str(report["os_id"])),
				os_version=str(report["os_version"]),
				cpu_count=int(report["cpu_count"]),
				memory_mib=int(report["memory_bytes"]) // MIB,
				has_kvm=report["has_kvm"] is True,
				public_network_interface=public_interface_name or next(iter(default_route_interfaces), ""),
				private_network_interface=interface_name,
				private_network_mtu=interface_mtu,
				disks=(
					*cls._parse_disks(report["block_devices"]["blockdevices"]),
					*cls._parse_disk_image(report["disk_image"]),
				),
				image_available_gib=int(report["image_available_bytes"]) // GIB,
			)
		except (ValueError, TypeError, KeyError) as error:
			raise HostReportError(f"The host probe printed an invalid report: {error!r}") from error

	@classmethod
	def from_dict(cls, values: Mapping[str, Any]) -> "HostReport":
		"""Return a report from its cached form."""
		return cls(**{**values, "disks": tuple(InspectedDisk(**disk) for disk in values["disks"])})

	@property
	def architecture(self) -> str | None:
		"""Return the Metal architecture of the machine."""
		return SUPPORTED_MACHINES.get(self.machine)

	@property
	def free_devices(self) -> list[str]:
		"""Return the disks that hold no data."""
		return [disk.device for disk in self.disks if not disk.busy_reason]

	@property
	def can_create_disk_image(self) -> bool:
		"""Report whether Atlas can create the disk image as the storage pool device."""
		has_disk_image = any(disk.device == DISK_IMAGE_PATH for disk in self.disks)
		return not self.free_devices and not has_disk_image and self.image_available_gib > 0

	@property
	def disk_image_default_size_gib(self) -> int:
		"""Return the suggested disk image size."""
		return self.image_available_gib * DISK_IMAGE_DEFAULT_PERCENT // 100

	@property
	def server_size_name(self) -> str:
		"""Return the Metal Server Size name, such as 32x128."""
		return f"{self.cpu_count}x{ceil(self.memory_mib / 1024)}"

	@property
	def server_image_name(self) -> str:
		"""Return the Metal Server Image name, such as Ubuntu 24.04."""
		return f"{self.os} {self.os_version}"

	def get_failures(self, private_ipv4_address: str) -> list[str]:
		"""Return one message for each host check that fails. The storage disk is chosen after these checks."""
		checks = (
			(self.architecture, f"Machine {self.machine} is not supported. Atlas needs x86_64."),
			(
				self.os_version in ACCEPTED_OS_VERSIONS.get(self.os, ()),
				f"{self.os} {self.os_version} is not a supported operating system.",
			),
			(self.has_kvm, "The host has no KVM device or no CPU virtualization flag."),
			(self.cpu_count > 0 and self.memory_mib > 0, "The host reported no CPU or memory."),
			(
				self.private_network_interface,
				f"No interface holds the private IPv4 address {private_ipv4_address}.",
			),
			(
				not self.private_network_interface or self.private_network_mtu >= MINIMUM_PRIVATE_NETWORK_MTU,
				f"Interface {self.private_network_interface} has MTU {self.private_network_mtu}."
				f" Atlas needs at least {MINIMUM_PRIVATE_NETWORK_MTU}.",
			),
		)
		return [message for is_passed, message in checks if not is_passed]

	@staticmethod
	def _find_interface(addresses: list[Mapping[str, Any]], address: str) -> tuple[str, int]:
		for interface in addresses:
			for info in interface.get("addr_info") or []:
				if info.get("family") == "inet" and info.get("local") == address:
					return str(interface["ifname"]), int(interface["mtu"])
		return "", 0

	@classmethod
	def _parse_disks(cls, block_devices: list[Mapping[str, Any]]) -> tuple[InspectedDisk, ...]:
		return tuple(
			InspectedDisk(
				device=str(device["name"]),
				size_gib=int(device.get("size") or 0) // GIB,
				busy_reason=cls._busy_reason(device),
			)
			for device in block_devices
			if device.get("type") == "disk" and not str(device["name"]).startswith("/dev/zram")
		)

	@staticmethod
	def _parse_disk_image(image: Mapping[str, Any] | None) -> tuple[InspectedDisk, ...]:
		if image is None:
			return ()

		reasons = (
			(not image["is_file"], "is not a regular file"),
			(image["fstype"], "has a file system or signature"),
		)
		return (
			InspectedDisk(
				device=str(image["name"]),
				size_gib=int(image["size"]) // GIB,
				busy_reason=", ".join(reason for is_busy, reason in reasons if is_busy),
			),
		)

	@staticmethod
	def _busy_reason(device: Mapping[str, Any]) -> str:
		checks = (
			(device.get("children"), "has partitions or holders"),
			(device.get("pttype"), "has a partition table"),
			(device.get("fstype"), "has a file system or signature"),
			(device.get("mountpoint"), "is mounted"),
			(device.get("ro") in (True, "1"), "is read-only"),
			(device.get("rm") in (True, "1"), "is removable"),
		)
		return ", ".join(reason for is_busy, reason in checks if is_busy)


class HostInspection:
	"""Inspect an operator-prepared host and register it as a Metal Server.

	The inspection lives only in the cache. Registration reads the report
	from the cache, so the browser never supplies host facts.
	"""

	script = "generic/inspect-host.sh"
	timeout_seconds = 120
	expiry_seconds = 1_800

	def __init__(self, inspection_id: str) -> None:
		self.inspection_id = inspection_id

	@property
	def cache_key(self) -> str:
		"""Return the cache key of this inspection."""
		return f"atlas:host-inspection:{self.inspection_id}"

	@classmethod
	def start(cls, public_ipv4_address: str, private_ipv4_address: str) -> str:
		"""Check the addresses, queue the host probe, and return the inspection ID."""
		validate_addresses(public_ipv4_address, private_ipv4_address)
		inspection = cls(frappe.generate_hash(length=16))
		inspection.store(
			{
				"status": "Inspecting",
				"public_ipv4_address": public_ipv4_address,
				"private_ipv4_address": private_ipv4_address,
			}
		)
		frappe.enqueue(
			run_host_inspection,
			inspection_id=inspection.inspection_id,
			timeout=cls.timeout_seconds * 2,
			job_id=f"atlas||host-inspection||{inspection.inspection_id}",
			deduplicate=True,
		)
		return inspection.inspection_id

	@property
	def state(self) -> dict[str, Any]:
		"""Return the cached state of this inspection."""
		state = frappe.cache.get_value(self.cache_key)
		if not state:
			frappe.throw(_("The host inspection expired. Inspect the host again."))
		return state

	def store(self, state: Mapping[str, Any]) -> None:
		"""Replace the cached state of this inspection."""
		frappe.cache.set_value(self.cache_key, dict(state), expires_in_sec=self.expiry_seconds)

	def run(self) -> None:
		"""Probe the host and cache the report or the probe error."""
		state = self.state
		try:
			output = self.get_probe_output(state["public_ipv4_address"])
			report = HostReport.parse(output, state["public_ipv4_address"], state["private_ipv4_address"])
		except HostReportError as error:
			self.store({**state, "status": "Failed", "error": str(error)})
			return

		failures = report.get_failures(state["private_ipv4_address"])
		self.store(
			{
				**state,
				"status": "Completed",
				"report": asdict(report),
				"failures": failures,
				"can_create_disk_image": report.can_create_disk_image and not failures,
				"disk_image_path": DISK_IMAGE_PATH,
				"disk_image_default_size_gib": report.disk_image_default_size_gib,
				"server_size_name": report.server_size_name,
				"server_image_name": report.server_image_name,
			}
		)

	def get_probe_output(self, public_ipv4_address: str) -> str:
		"""Run the read-only probe as root and return its output."""
		try:
			result = SSHRunner(public_ipv4_address).run_script(
				self.script, data={"DISK_IMAGE_PATH": DISK_IMAGE_PATH}, timeout_seconds=self.timeout_seconds
			)
		except (OSError, subprocess.TimeoutExpired) as error:
			raise HostReportError(
				f"Atlas could not connect to root@{public_ipv4_address}: {error}"
			) from error
		if not result.is_success:
			raise HostReportError(f"The host probe failed:\n{result.output[-2000:]}")
		return result.output

	def start_disk_image_creation(self, size_gib: int) -> None:
		"""Check the requested size and queue the disk image creation."""
		state = self.state
		if not state.get("can_create_disk_image"):
			frappe.throw(_("Atlas can create a disk image only on a host that has no free disk."))

		report = HostReport.from_dict(state["report"])
		if not 1 <= size_gib <= report.image_available_gib:
			frappe.throw(
				_("Disk image size must be between 1 and {0} GiB.").format(report.image_available_gib)
			)

		self.store({**state, "status": "Inspecting"})
		frappe.enqueue(
			run_disk_image_creation,
			inspection_id=self.inspection_id,
			size_gib=size_gib,
			timeout=self.timeout_seconds * 3,
			job_id=f"atlas||host-disk-image||{self.inspection_id}",
			deduplicate=True,
		)

	def create_disk_image(self, size_gib: int) -> None:
		"""Create the disk image on the host and inspect the host again."""
		state = self.state
		try:
			result = SSHRunner(state["public_ipv4_address"]).run_script(
				"generic/create-disk-image.sh",
				data={"DISK_IMAGE_PATH": DISK_IMAGE_PATH, "DISK_IMAGE_SIZE_GIB": size_gib},
				timeout_seconds=self.timeout_seconds,
			)
		except (OSError, subprocess.TimeoutExpired) as error:
			result = SSHResult(output=str(error), exit_code=None)
		if not result.is_success:
			self.store(
				{
					**state,
					"status": "Failed",
					"error": f"Disk image creation failed:\n{result.output[-2000:]}",
				}
			)
			return

		self.run()

	def register(self, storage_pool_device: str, provider_server_id: str | None) -> "MetalServer":
		"""Create the Metal Server for a passed inspection and start its provisioning."""
		state = self.state
		if state["status"] != "Completed" or state["failures"]:
			frappe.throw(_("Only an inspection without failures can create a Metal Server."))

		report = HostReport.from_dict(state["report"])
		if storage_pool_device not in report.free_devices:
			frappe.throw(_("Storage pool device {0} is not a free disk.").format(storage_pool_device))

		with frappe.db.advisory_lock(f"{frappe.db.cur_db_name}:host-registration"):
			validate_addresses(state["public_ipv4_address"], state["private_ipv4_address"])
			server = self.insert_server(state, report, storage_pool_device, provider_server_id)
			frappe.cache.delete_value(self.cache_key)
		return server

	def insert_server(
		self,
		state: Mapping[str, Any],
		report: HostReport,
		storage_pool_device: str,
		provider_server_id: str | None,
	) -> "MetalServer":
		"""Insert the Pending Metal Server with the inspected host facts."""
		disk = next(disk for disk in report.disks if disk.device == storage_pool_device)
		server: "MetalServer" = frappe.new_doc("Metal Server")
		server.update(
			{
				"title": report.hostname,
				"provider_server_id": provider_server_id
				or f"generic-{datetime.now(UTC):%d-%m-%Y}-{frappe.generate_hash(length=6)}",
				"server_size": ensure_server_size(report, disk.size_gib),
				"server_image": ensure_server_image(report),
				"architecture": report.architecture,
				"public_ipv4_address": state["public_ipv4_address"],
				"private_ipv4_address": state["private_ipv4_address"],
				"public_network_interface": report.public_network_interface,
				"private_network_interface": report.private_network_interface,
				"provider_metadata": frappe.as_json(
					{
						"hostname": report.hostname,
						"os": report.os,
						"os_version": report.os_version,
						"cpu_count": report.cpu_count,
						"memory_mib": report.memory_mib,
						"private_network_mtu": report.private_network_mtu,
						"storage_pool_device": storage_pool_device,
						"storage_pool_size_gib": disk.size_gib,
						"inspected_at": datetime.now(UTC).isoformat(timespec="seconds"),
					}
				),
				"status": "Pending",
			}
		)
		server.insert(ignore_permissions=True)
		return server


def validate_addresses(public_ipv4_address: str, private_ipv4_address: str) -> None:
	"""Check both host addresses and refuse ones that an active Metal Server uses."""
	from atlas.atlas.core.server_providers.generic import GenericProvider

	settings = frappe.get_single("Atlas Settings")
	if not isinstance(settings.server_provider_controller, GenericProvider):
		frappe.throw(_("Host registration needs the Generic server provider."))

	for label, address in (("Public", public_ipv4_address), ("Private", private_ipv4_address)):
		try:
			ipaddress.IPv4Address(address)
		except ipaddress.AddressValueError:
			frappe.throw(_("{0} IPv4 address {1} is not valid.").format(label, address))

	network = ipaddress.ip_network(settings.private_network_cidr, strict=False)
	if ipaddress.IPv4Address(private_ipv4_address) not in network:
		frappe.throw(_("Private IPv4 address must be inside {0}.").format(network))

	for field, address in (
		("public_ipv4_address", public_ipv4_address),
		("private_ipv4_address", private_ipv4_address),
	):
		server = frappe.db.exists("Metal Server", {field: address, "status": ["!=", "Deleted"]})
		if server:
			frappe.throw(_("Metal Server {0} already uses {1}.").format(server, address))


def ensure_server_size(report: HostReport, disk_gib: int) -> str:
	"""Return the Metal Server Size for a host, and create it when absent."""
	name = report.server_size_name
	if frappe.db.exists("Metal Server Size", name):
		size = frappe.get_doc("Metal Server Size", name)
		if size.architecture != report.architecture or size.cpu_count != report.cpu_count:
			frappe.throw(_("Metal Server Size {0} does not match this host.").format(name))
		return name

	frappe.get_doc(
		{
			"doctype": "Metal Server Size",
			"name": name,
			"enabled": 1,
			"architecture": report.architecture,
			"cpu_count": report.cpu_count,
			"memory_mib": report.memory_mib,
			"disk_gib": disk_gib,
		}
	).insert(ignore_permissions=True)
	return name


def ensure_server_image(report: HostReport) -> str:
	"""Return the Metal Server Image for a host, and create it when absent."""
	name = report.server_image_name
	if frappe.db.exists("Metal Server Image", name):
		image = frappe.get_doc("Metal Server Image", name)
		if image.os != report.os or image.os_version != report.os_version:
			frappe.throw(_("Metal Server Image {0} does not match this host.").format(name))
		return name

	frappe.get_doc(
		{
			"doctype": "Metal Server Image",
			"name": name,
			"enabled": 1,
			"os": report.os,
			"os_version": report.os_version,
		}
	).insert(ignore_permissions=True)
	return name


@run_as_admin
def run_host_inspection(inspection_id: str) -> None:
	"""Run one queued host inspection."""
	HostInspection(inspection_id).run()


@run_as_admin
def run_disk_image_creation(inspection_id: str, size_gib: int) -> None:
	"""Run one queued disk image creation."""
	HostInspection(inspection_id).create_disk_image(size_gib)
