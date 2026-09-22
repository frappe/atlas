from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

import frappe

from atlas.atlas.core.parsing import strict_bool

EGRESS_MODES = ("uplink", "mesh", "none")
MINIMUM_CPU_MILLICORES = 100
MAXIMUM_CPU_MILLICORES = 32_000
MAXIMUM_SLEEP_AFTER_IDLE_SECONDS = 9_223_372_036
FIREWALL_PROTOCOLS = ("any", "tcp", "udp", "icmp")
MAXIMUM_FIREWALL_PREFIXES = 50
FIREWALL_PORTS_PATTERN = re.compile(r"^[0-9]+(?:-[0-9]+)?$")


@dataclass(frozen=True, slots=True)
class VirtualMachineShape:
	"""The CPU, memory, disk, and idle shutdown values of one VM."""

	cpu_millicores: int
	memory_mib: int
	disk_mib: int
	sleep_after_idle_seconds: int

	def has_same_resources(self, other: VirtualMachineShape) -> bool:
		"""Report whether CPU, memory, and disk size match."""
		return (self.cpu_millicores, self.memory_mib, self.disk_mib) == (
			other.cpu_millicores,
			other.memory_mib,
			other.disk_mib,
		)

	@property
	def compute(self) -> dict[str, int]:
		"""Return the complete Metal compute object."""
		return {
			"cpu_millicores": self.cpu_millicores,
			"memory_mib": self.memory_mib,
			"sleep_after_idle_seconds": self.sleep_after_idle_seconds,
		}

	@property
	def migration_resize(self) -> dict[str, int]:
		"""Return the resize object of a Metal migration request."""
		return {
			"cpu_millicores": self.cpu_millicores,
			"memory_mib": self.memory_mib,
			"disk_mib": self.disk_mib,
		}

	@property
	def resize(self) -> dict[str, int]:
		"""Return the complete Metal in-place resize object."""
		return {**self.compute, "disk_mib": self.disk_mib}


@dataclass(frozen=True, slots=True)
class FirewallRule:
	"""Store one validated firewall allow rule."""

	protocol: str
	ports: str
	cidrs: tuple[str, ...]

	@classmethod
	def from_value(cls, value: object) -> FirewallRule:
		"""Parse one firewall rule."""
		if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
			raise ValueError("Each firewall rule must be an object.")
		unknown = set(value) - {"protocol", "ports", "cidrs"}
		if unknown:
			raise ValueError(f"Unknown firewall rule field: {sorted(unknown)[0]}.")

		protocol = value.get("protocol")
		if protocol not in FIREWALL_PROTOCOLS:
			raise ValueError("Firewall protocol must be any, tcp, udp, or icmp.")
		ports = value.get("ports") or ""
		if not isinstance(ports, str):
			raise ValueError("Firewall ports must be a string.")
		cls.validate_ports(protocol, ports)

		cidr_values = value.get("cidrs")
		if not isinstance(cidr_values, list) or not cidr_values:
			raise ValueError("Firewall CIDRs must be a non-empty list.")
		cidrs = tuple(cls.canonical_cidr(cidr) for cidr in cidr_values)
		return cls(protocol=protocol, ports=ports, cidrs=cidrs)

	@staticmethod
	def validate_ports(protocol: str, ports: str) -> None:
		"""Reject a port value that Metal cannot apply."""
		if not ports:
			return
		if protocol not in {"tcp", "udp"}:
			raise ValueError("Firewall ports are valid only for tcp or udp.")
		if not FIREWALL_PORTS_PATTERN.fullmatch(ports):
			raise ValueError("Firewall ports must be one port or one inclusive range.")

		start_value, separator, end_value = ports.partition("-")
		start = int(start_value)
		end = int(end_value) if separator else start
		if str(start) != start_value or (separator and str(end) != end_value):
			raise ValueError("Firewall ports must not contain leading zeros.")
		if start < 1 or end > 65535 or start > end:
			raise ValueError("Firewall ports must be between 1 and 65535 in ascending order.")

	@staticmethod
	def canonical_cidr(value: object) -> str:
		"""Return one canonical IP prefix."""
		if not isinstance(value, str):
			raise ValueError("Each firewall CIDR must be a string.")
		try:
			network = ipaddress.ip_network(value, strict=True)
		except ValueError as error:
			raise ValueError(f"Firewall CIDR {value!r} must be a canonical IPv4 or IPv6 prefix.") from error
		return str(network)

	def as_dict(self) -> dict[str, object]:
		"""Return a JSON-compatible rule."""
		return {"protocol": self.protocol, "ports": self.ports, "cidrs": list(self.cidrs)}


@dataclass(frozen=True, slots=True)
class FirewallConfiguration:
	"""Store the complete desired firewall configuration."""

	enabled: bool = False
	inbound: tuple[FirewallRule, ...] = ()
	outbound: tuple[FirewallRule, ...] = ()

	@classmethod
	def from_value(cls, value: object | None) -> FirewallConfiguration:
		"""Parse and validate one complete firewall configuration."""
		if value is None:
			return cls()
		if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
			raise ValueError("Firewall must be an object.")
		unknown = set(value) - {"enabled", "inbound", "outbound"}
		if unknown:
			raise ValueError(f"Unknown firewall field: {sorted(unknown)[0]}.")

		inbound = cls.rules_from_value(value.get("inbound", []), "inbound")
		outbound = cls.rules_from_value(value.get("outbound", []), "outbound")
		if sum(len(rule.cidrs) for rule in (*inbound, *outbound)) > MAXIMUM_FIREWALL_PREFIXES:
			raise ValueError(f"Firewall must not exceed {MAXIMUM_FIREWALL_PREFIXES} prefix entries.")
		return cls(
			enabled=strict_bool(value.get("enabled"), "firewall.enabled"),
			inbound=inbound,
			outbound=outbound,
		)

	@staticmethod
	def rules_from_value(value: object, direction: str) -> tuple[FirewallRule, ...]:
		"""Parse one direction's complete rule list."""
		if not isinstance(value, list):
			raise ValueError(f"Firewall {direction} rules must be a list.")
		return tuple(FirewallRule.from_value(rule) for rule in value)

	def as_dict(self) -> dict[str, object]:
		"""Return a JSON-compatible firewall configuration."""
		return {
			"enabled": self.enabled,
			"inbound": [rule.as_dict() for rule in self.inbound],
			"outbound": [rule.as_dict() for rule in self.outbound],
		}


@dataclass(frozen=True, slots=True)
class VirtualMachineCreateRequest:
	"""Store the validated values for one virtual machine request."""

	virtual_machine_image: str
	cpu_millicores: int
	memory_mib: int
	disk_mib: int
	tenant_id: int
	is_privileged: bool = False
	is_termination_protected: bool = False
	hostname: str = ""
	ssh_keys: tuple[str, ...] = ()
	user_data: str = ""
	egress: str = "uplink"
	sleep_after_idle_seconds: int = 0
	disk_throughput_mibps: int = 0
	disk_iops: int = 0
	private_network_throughput_mibps: int = 0
	public_network_throughput_mibps: int = 0
	server_ip_address: str | None = None
	metadata: dict[str, str] = field(default_factory=dict)
	firewall: FirewallConfiguration = field(default_factory=FirewallConfiguration)

	@classmethod
	def from_value(cls, value: str | dict[str, Any]) -> VirtualMachineCreateRequest:
		"""Parse and validate one virtual machine request."""
		payload = frappe.parse_json(value) if isinstance(value, str) else value
		if not isinstance(payload, dict):
			raise ValueError("Virtual Machine request must be a JSON object.")

		image = payload.get("virtual_machine_image")
		if not isinstance(image, str) or not image:
			raise ValueError("Virtual Machine Image is required.")

		cpu_millicores = cls.positive_integer(payload, "cpu_millicores", "CPU millicores")
		if cpu_millicores < MINIMUM_CPU_MILLICORES:
			raise ValueError(f"CPU millicores must be at least {MINIMUM_CPU_MILLICORES}.")
		if cpu_millicores > MAXIMUM_CPU_MILLICORES:
			raise ValueError(f"CPU millicores must not exceed {MAXIMUM_CPU_MILLICORES}.")
		memory_mib = cls.positive_integer(payload, "memory_mib", "Memory")
		disk_mib = cls.positive_integer(payload, "disk_mib", "Disk")
		tenant_id = payload.get("tenant_id")
		if not isinstance(tenant_id, int) or isinstance(tenant_id, bool) or not 0 <= tenant_id <= 0xFFFFFFFF:
			raise ValueError("Tenant ID must be a 32-bit unsigned integer.")

		egress = payload.get("egress") or "uplink"
		if egress not in EGRESS_MODES:
			raise ValueError("Egress must be uplink, mesh, or none.")

		public_network_throughput_mibps = cls.non_negative_integer(payload, "public_network_throughput_mibps")
		server_ip_address = payload.get("server_ip_address") or None
		if egress != "uplink" and server_ip_address:
			raise ValueError("A public IPv4 address requires uplink egress.")

		sleep_after_idle_seconds = cls.non_negative_integer(payload, "sleep_after_idle_seconds")
		if sleep_after_idle_seconds > MAXIMUM_SLEEP_AFTER_IDLE_SECONDS:
			raise ValueError("sleep_after_idle_seconds is too large.")

		return cls(
			virtual_machine_image=image,
			cpu_millicores=cpu_millicores,
			memory_mib=memory_mib,
			disk_mib=disk_mib,
			tenant_id=tenant_id,
			is_privileged=strict_bool(payload.get("is_privileged"), "is_privileged"),
			is_termination_protected=strict_bool(
				payload.get("is_termination_protected"), "is_termination_protected"
			),
			hostname=str(payload.get("hostname") or ""),
			ssh_keys=cls.ssh_keys_tuple(payload),
			user_data=str(payload.get("user_data") or ""),
			egress=egress,
			sleep_after_idle_seconds=sleep_after_idle_seconds,
			disk_throughput_mibps=cls.non_negative_integer(payload, "disk_throughput_mibps"),
			disk_iops=cls.non_negative_integer(payload, "disk_iops"),
			private_network_throughput_mibps=cls.non_negative_integer(
				payload, "private_network_throughput_mibps"
			),
			public_network_throughput_mibps=public_network_throughput_mibps,
			server_ip_address=server_ip_address,
			metadata=cls.metadata_map(payload),
			firewall=FirewallConfiguration.from_value(payload.get("firewall")),
		)

	@staticmethod
	def ssh_keys_tuple(payload: dict[str, Any]) -> tuple[str, ...]:
		"""Return the authorized keys from a list or from a newline-separated block."""
		value = payload.get("ssh_keys") or []
		if isinstance(value, str):
			value = value.splitlines()
		if not isinstance(value, list) or any(not isinstance(key, str) for key in value):
			raise ValueError("SSH keys must be a list of strings.")

		return tuple(key.strip() for key in value if key.strip())

	@staticmethod
	def non_negative_integer(payload: dict[str, Any], field_name: str) -> int:
		"""Return one optional non-negative integer."""
		value = payload.get(field_name) or 0
		if not isinstance(value, int) or isinstance(value, bool) or value < 0:
			raise ValueError(f"{field_name} must be a non-negative integer.")
		return value

	@staticmethod
	def metadata_map(payload: dict[str, Any]) -> dict[str, str]:
		"""Return validated metadata with clean keys."""
		value = payload.get("metadata") or {}
		if not isinstance(value, dict):
			raise ValueError("Metadata must be a string-to-string map.")

		metadata: dict[str, str] = {}
		for key, item in value.items():
			if not isinstance(key, str) or not isinstance(item, str):
				raise ValueError("Metadata keys and values must be strings.")
			key = key.strip()
			if not key:
				raise ValueError("Metadata key cannot be empty.")
			metadata[key] = item
		return metadata

	@staticmethod
	def positive_integer(payload: dict[str, Any], field_name: str, label: str) -> int:
		"""Return one required positive integer."""
		value = payload.get(field_name)
		if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
			raise ValueError(f"{label} must be a positive integer.")
		return value
