from __future__ import annotations

import hashlib
import re
import shlex
import sqlite3
from contextlib import closing, contextmanager
from ipaddress import IPv4Address, IPv6Address
from ipaddress import ip_address as parse_address
from pathlib import Path
from typing import TYPE_CHECKING, override

import frappe
from cryptography import x509

from atlas.atlas.core.dns_providers import register
from atlas.atlas.core.dns_providers.base import MESH_NETWORK, DnsProvider, DnsProviderError
from atlas.atlas.core.ssh import SSHRunner

if TYPE_CHECKING:
	from collections.abc import Iterator

	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


@register
class StaticProvider(DnsProvider):
	"""Store development DNS locally and deliver it to explicitly selected VMs."""

	provider_type = "Static"
	credential_fields = ()

	@override
	def upsert_service_record(self, name: str, address: IPv4Address | IPv6Address, ttl: int) -> None:
		if frappe.conf.get("dev_mesh_only_proxy"):
			from atlas.service.doctype.proxy_server.proxy_server import is_mesh_only_proxy_enabled

			is_mesh_only_proxy_enabled()
			self._is_internal_service_record(name)
			if address not in MESH_NETWORK:
				raise DnsProviderError("Mesh-only service records require a WG Mesh address.")
			self.upsert_record("AAAA", name, [str(address)], ttl, "A")
			return
		super().upsert_service_record(name, address, ttl)

	def __init__(self, settings: AtlasSettings | None = None) -> None:
		super().__init__(settings)
		self.validate_settings()
		self.path = Path(frappe.get_site_path("private", "static-dns.sqlite3"))

	@override
	def validate_settings(self) -> None:
		if not frappe.conf.developer_mode or not self.domain_name.endswith(".test"):
			raise DnsProviderError("Static DNS requires developer mode and a .test wildcard domain.")
		self._name(self.domain_name)

	@override
	def validate_credentials(self) -> bool:
		self.validate_settings()
		return True

	@override
	def bootstrap(self) -> None:
		self.upsert_cname_record(f"*.{self.domain_name}", f"proxy.{self.domain_name}", ttl=3600)
		self.settings.is_dns_setup_completed = 1
		self.settings.save()

	@override
	def create_zone(self, domain: str) -> str:
		return self._name(domain)

	@override
	def find_public_zone_id(self, domain: str) -> None:
		return None

	@override
	def upsert_record(
		self,
		record_type: str,
		name: str,
		values: list[str],
		ttl: int = 300,
		replaced_type: str | None = None,
	) -> None:
		name = self._name(name)
		if record_type not in {"A", "AAAA", "CNAME"} or not values or ttl <= 0:
			raise DnsProviderError(
				"Static DNS supports nonempty A, AAAA and CNAME records with positive TTL."
			)
		if record_type == "CNAME":
			if len(values) != 1:
				raise DnsProviderError("A CNAME needs exactly one local target.")
			values = [self._name(values[0])]
		else:
			try:
				addresses = [parse_address(value) for value in values]
			except ValueError as error:
				raise DnsProviderError("Invalid static DNS address.") from error
			if any(address.version != (4 if record_type == "A" else 6) for address in addresses):
				raise DnsProviderError("Static DNS address does not match the record type.")
			values = [str(address) for address in addresses]
		with self._connection() as connection:
			if replaced_type:
				connection.execute("DELETE FROM records WHERE name=? AND type=?", (name, replaced_type))
			other_types = connection.execute(
				"SELECT DISTINCT type FROM records WHERE name=?", (name,)
			).fetchall()
			if any((row[0] == "CNAME") != (record_type == "CNAME") for row in other_types):
				raise DnsProviderError("A CNAME cannot share a name with address records.")
			connection.execute("DELETE FROM records WHERE name=? AND type=?", (name, record_type))
			connection.executemany(
				"INSERT INTO records VALUES (?, ?, '', ?, ?)",
				[(name, record_type, value, ttl) for value in values],
			)
			self._resolve(name, connection, set(), wildcard=False)

	@override
	def remove_record(self, record_type: str, name: str) -> None:
		with self._connection() as connection:
			connection.execute("DELETE FROM records WHERE name=? AND type=?", (self._name(name), record_type))

	@override
	def create_https_health_check(self, ip_address: str, domain: str, path: str) -> str:
		return "static-" + hashlib.sha256(f"{ip_address}:{domain}:{path}".encode()).hexdigest()[:16]

	@override
	def remove_health_check(self, health_check_id: str) -> None:
		pass

	@override
	def upsert_multivalue_a_record(
		self, name: str, identifier: str, ip_address: str, health_check_id: str, ttl: int = 120
	) -> None:
		name = self._name(name)
		try:
			address = parse_address(ip_address)
		except ValueError as error:
			raise DnsProviderError("Invalid multivalue address.") from error
		if address.version != 4 or not identifier or ttl <= 0:
			raise DnsProviderError("A multivalue record needs an IPv4 address, identifier and positive TTL.")
		with self._connection() as connection:
			if connection.execute("SELECT 1 FROM records WHERE name=? AND type='CNAME'", (name,)).fetchone():
				raise DnsProviderError("A CNAME cannot share a name with address records.")
			connection.execute(
				"DELETE FROM records WHERE name=? AND type='A' AND identifier=?", (name, identifier)
			)
			connection.execute(
				"INSERT INTO records VALUES (?, 'A', ?, ?, ?)", (name, identifier, str(address), ttl)
			)

	@override
	def remove_multivalue_a_record(self, name: str, identifier: str) -> None:
		with self._connection() as connection:
			connection.execute(
				"DELETE FROM records WHERE name=? AND type='A' AND identifier=?",
				(self._name(name), identifier),
			)

	def resolve(self, name: str) -> list[str]:
		"""Resolve one local name, including CNAME and wildcard records."""
		with self._connection() as connection:
			return self._resolve(self._name(name), connection, set())

	def render_dnsmasq(self) -> str:
		"""Render an isolated local resolver for the development domain."""
		lines = [
			"port=5353",
			"listen-address=127.0.0.1",
			"bind-interfaces",
			"no-resolv",
			f"local=/{self.domain_name}/",
		]
		with self._connection() as connection:
			for (name,) in connection.execute("SELECT DISTINCT name FROM records ORDER BY name"):
				for address in self._resolve(name, connection, set()):
					if name.startswith("*."):
						lines.append(f"address=/{name[2:]}/{address}")
					else:
						lines.append(f"host-record={name},{address}")
		return "\n".join(lines) + "\n"

	def configure_virtual_machine(self, virtual_machine: VirtualMachine, ca_pem: str = "") -> None:
		"""Install local resolution and optional public CA trust through normal VM SSH."""
		self.validate_settings()
		configuration = self.render_dnsmasq()
		resolver = f"[Resolve]\nDNS=127.0.0.1:5353\nDomains=~{self.domain_name}\n"
		command = f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends dnsmasq-base
install -d /etc/atlas /etc/systemd/resolved.conf.d
printf %s {shlex.quote(configuration)} > /etc/atlas/static-dns.conf
printf %s {shlex.quote(resolver)} > /etc/systemd/resolved.conf.d/atlas-static-dns.conf
cat > /etc/systemd/system/atlas-static-dns.service <<'UNIT'
[Unit]
After=network.target
[Service]
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/atlas/static-dns.conf
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
/usr/sbin/dnsmasq --test --conf-file=/etc/atlas/static-dns.conf
systemctl daemon-reload
systemctl enable atlas-static-dns.service
systemctl restart atlas-static-dns.service
systemctl restart systemd-resolved
"""
		if ca_pem:
			try:
				certificates = x509.load_pem_x509_certificates(ca_pem.encode())
				if (
					"PRIVATE KEY" in ca_pem
					or len(certificates) != 1
					or not certificates[0].extensions.get_extension_for_class(x509.BasicConstraints).value.ca
				):
					raise ValueError("Expected one public CA certificate")
			except (ValueError, x509.ExtensionNotFound) as error:
				raise DnsProviderError("Static DNS trust requires one public CA certificate.") from error
			command += f"printf %s {shlex.quote(ca_pem)} > /usr/local/share/ca-certificates/atlas-static-dns.crt\nupdate-ca-certificates\n"
		result = SSHRunner(
			virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
		).run_command(command, timeout_seconds=300)
		if not result.is_success:
			raise DnsProviderError(f"Static DNS delivery failed for {virtual_machine.name}: {result.output}")

	@contextmanager
	def _connection(self) -> Iterator[sqlite3.Connection]:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		with closing(sqlite3.connect(self.path)) as connection, connection:
			self.path.chmod(0o600)
			connection.execute(
				"CREATE TABLE IF NOT EXISTS records (name TEXT, type TEXT, identifier TEXT, value TEXT, ttl INTEGER, UNIQUE(name, type, identifier, value))"
			)
			yield connection

	def _name(self, name: str) -> str:
		name = name.rstrip(".").lower()
		labels = name.removeprefix("*.").split(".")
		if len(name) > 253 or any(
			not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
		):
			raise DnsProviderError("Invalid static DNS name.")
		if name != self.domain_name and not name.endswith(f".{self.domain_name}"):
			raise DnsProviderError("Static DNS names must stay in the development domain.")
		return name

	def _resolve(
		self, name: str, connection: sqlite3.Connection, visited: set[str], wildcard: bool = True
	) -> list[str]:
		if name in visited:
			raise DnsProviderError("Static DNS CNAME cycle.")
		visited.add(name)
		rows = connection.execute(
			"SELECT type, value FROM records WHERE name=? ORDER BY value", (name,)
		).fetchall()
		if wildcard and not rows and name != self.domain_name:
			rows = connection.execute(
				"SELECT type, value FROM records WHERE name=? ORDER BY value", (f"*.{self.domain_name}",)
			).fetchall()
		addresses = []
		for record_type, value in rows:
			addresses.extend(
				self._resolve(value, connection, visited.copy(), wildcard=wildcard)
				if record_type == "CNAME"
				else [value]
			)
		return sorted(set(addresses))
