from __future__ import annotations

import hashlib
import json
from datetime import UTC, timedelta
from functools import cached_property
from string import Template
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import bcrypt
import frappe
from frappe import _
from frappe.utils import get_datetime, get_system_timezone

from atlas.atlas.core.mesh_address import get_region_mesh_address_prefix

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.service.doctype.proxy_server.proxy_server import ProxyServer

CONFIG_PATH = "/etc/atlas/proxy-control.toml"
APPLY_COMMAND = "/opt/atlas/proxy-control/bin/proxy-control"
DAEMON_UNIT = "atlas-proxy-control.service"
DAEMON_SOCKET_UNIT = "atlas-proxy-control.socket"
AUTO_PROXY_HOST_PREFIXES = ("site-", "*-vm-")

CONFIG_TEMPLATE = Template(
	"""[control]
domain = "$cluster_domain"
node_domain = "$node_domain"

[auto_proxy]
address_prefix = "$auto_proxy_prefix"
host_prefixes = $auto_proxy_host_prefixes

[auth]
password_hash = "$password_hash"
previous_password_hash = "$previous_password_hash"
previous_password_valid_until = $previous_password_valid_until
jwks_url = "$jwks_url"
jwks_audience_id = "$jwks_audience_id"
jwks_issuers = $jwks_issuers

[cluster]
node_id = "$node_id"
password = "$cluster_password"
previous_password = "$previous_cluster_password"
previous_password_valid_until = $previous_password_valid_until
peers = $peers

[tls]
wildcard_domain = "$wildcard_domain"
fullchain_pem = '''
$certificate
'''
private_key_pem = '''
$private_key
'''
"""
)

WRITE_COMMAND_TEMPLATE = Template(
	"""set -eu
install -d -m 0750 /etc/atlas
install -m 0600 /dev/null $temporary_path
cat > $temporary_path <<'ATLAS_PROXY_CONFIG_END'
$content
ATLAS_PROXY_CONFIG_END
mv -f $temporary_path $config_path"""
)

APPLY_COMMAND_TEMPLATE = Template(
	"""set -eu
$apply_command
systemctl enable --now $socket_unit
systemctl enable $daemon_unit
systemctl restart $daemon_unit"""
)


class ProxyConfiguration:
	"""Render a Proxy Server configuration file."""

	def __init__(
		self,
		proxy_server: "ProxyServer",
		cluster_proxy_servers: list["ProxyServer"] | None = None,
	) -> None:
		self.proxy_server = proxy_server
		self.settings: AtlasSettings = frappe.get_single("Atlas Settings")
		self.cluster_proxy_servers = cluster_proxy_servers

	@property
	def wildcard_domain(self) -> str:
		"""Return the name the certificate covers, which also sets the proxy region."""
		return f"*.{self.settings.wildcard_domain}"

	@property
	def auto_proxy_prefix(self) -> str:
		"""Return the regional prefix that auto proxy routes resolve under."""
		return get_region_mesh_address_prefix(self.settings.region_id)

	@property
	def content(self) -> str:
		"""Return the complete configuration file."""
		certificate = self.settings.get_password("wildcard_tls_certificate", raise_exception=False)
		private_key = self.settings.get_password("wildcard_tls_private_key", raise_exception=False)
		if not certificate or not private_key:
			frappe.throw(_("Atlas Settings holds no wildcard TLS certificate to send to a proxy."))

		return CONFIG_TEMPLATE.substitute(
			cluster_domain=self.cluster_domain,
			node_domain=self.proxy_server.get_domain(),
			auto_proxy_prefix=self.auto_proxy_prefix,
			auto_proxy_host_prefixes=json.dumps(AUTO_PROXY_HOST_PREFIXES),
			password_hash=self.password_hash,
			previous_password_hash=self.previous_password_hash,
			previous_password_valid_until=self.previous_password_valid_until,
			jwks_url=self.settings.jwks_url,
			jwks_audience_id=self.settings.proxy_audience_id,
			jwks_issuers=json.dumps(["central", self.settings.issuer]),
			node_id=self.proxy_server.name,
			cluster_password=self.cluster_password,
			previous_cluster_password=self.previous_cluster_password,
			peers=self.peers_toml,
			wildcard_domain=self.wildcard_domain,
			certificate=certificate.strip(),
			private_key=private_key.strip(),
		)

	@property
	def cluster_password(self) -> str:
		"""Return the current regional cluster password."""
		password = self.settings.get_password("proxy_cluster_password", raise_exception=False)
		if not password:
			frappe.throw(_("Atlas Settings holds no proxy cluster password."))
		return password

	@property
	def previous_cluster_password(self) -> str:
		"""Return the previous regional cluster password."""
		return self.settings.get_password("previous_proxy_cluster_password", raise_exception=False) or ""

	@cached_property
	def password_hash(self) -> str:
		"""Return the bcrypt hash of the regional proxy password."""
		return bcrypt.hashpw(self.cluster_password.encode(), bcrypt.gensalt()).decode()

	@cached_property
	def previous_password_hash(self) -> str:
		"""Return the bcrypt hash of the previous regional proxy password."""
		if not self.previous_cluster_password:
			return ""

		return bcrypt.hashpw(self.previous_cluster_password.encode(), bcrypt.gensalt()).decode()

	@property
	def previous_password_valid_until(self) -> int:
		"""Return the Unix time when the previous password expires."""
		rotated_on = self.settings.proxy_cluster_password_rotated_on
		if not self.previous_cluster_password or not rotated_on:
			return 0

		rotation = get_datetime(rotated_on).replace(tzinfo=ZoneInfo(get_system_timezone()))
		return int((rotation.astimezone(UTC) + timedelta(minutes=10)).timestamp())

	@property
	def cluster_domain(self) -> str:
		"""Return the regional proxy cluster domain."""
		return f"proxy.{self.settings.wildcard_domain}"

	@property
	def peers(self) -> list[dict[str, str]]:
		"""Return configured proxy cluster members."""
		proxy_servers = self.cluster_proxy_servers
		if proxy_servers is None:
			names = frappe.get_all(
				"Proxy Server",
				filters={"status": ["in", ("Active", "Provisioning")]},
				pluck="name",
			)
			proxy_servers = [frappe.get_doc("Proxy Server", name) for name in names]
		if self.proxy_server.name not in {item.name for item in proxy_servers}:
			proxy_servers.append(self.proxy_server)
		return [
			{
				"node_id": proxy_server.name,
				"address": f"https://{proxy_server.get_domain()}",
			}
			for proxy_server in sorted(proxy_servers, key=lambda item: item.name)
		]

	@property
	def peers_toml(self) -> str:
		"""Return the cluster peers as a TOML array."""
		items = (
			f"{{ node_id = {json.dumps(peer['node_id'])}, address = {json.dumps(peer['address'])} }}"
			for peer in self.peers
		)
		return f"[{', '.join(items)}]"

	@property
	def digest(self) -> str:
		"""Return the digest of the configuration and its template."""
		values = (
			CONFIG_TEMPLATE.template,
			self.proxy_server.get_domain(),
			self.cluster_domain,
			self.wildcard_domain,
			self.auto_proxy_prefix,
			json.dumps(AUTO_PROXY_HOST_PREFIXES),
			self.cluster_password,
			self.previous_cluster_password,
			json.dumps(self.peers, sort_keys=True),
			self.settings.jwks_url,
			self.settings.proxy_audience_id,
			self.settings.issuer,
			self.settings.get_password("wildcard_tls_certificate", raise_exception=False) or "",
			self.settings.get_password("wildcard_tls_private_key", raise_exception=False) or "",
		)
		return hashlib.sha256("\0".join(values).encode()).hexdigest()

	def get_write_command(self) -> str:
		"""Return the command that writes the configuration file."""
		return WRITE_COMMAND_TEMPLATE.substitute(
			config_path=CONFIG_PATH,
			temporary_path=f"{CONFIG_PATH}.tmp",
			content=self.content,
		)

	def get_apply_command(self) -> str:
		"""Return the command that applies the configuration."""
		return APPLY_COMMAND_TEMPLATE.substitute(
			apply_command=APPLY_COMMAND, socket_unit=DAEMON_SOCKET_UNIT, daemon_unit=DAEMON_UNIT
		)


def push_configuration_to_active_proxies() -> None:
	"""Send the current configuration to each active Proxy Server."""
	for name in frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue_doc(
			"Proxy Server",
			name,
			"_push_configuration",
			queue="long",
			timeout=600,
			job_id=f"atlas||proxy-server||configure||{name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def reconcile_proxy_configurations() -> None:
	"""Retry configuration delivery for active proxy servers."""
	for name in frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name"):
		proxy_server: ProxyServer = frappe.get_doc("Proxy Server", name)
		if proxy_server.pushed_config_hash != ProxyConfiguration(proxy_server).digest:
			proxy_server.enqueue_configuration_push(enqueue_after_commit=False)
