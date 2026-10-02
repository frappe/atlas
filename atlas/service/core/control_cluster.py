from __future__ import annotations

import json
from datetime import UTC, timedelta
from functools import cached_property
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import bcrypt
import frappe
from frappe import _
from frappe.utils import get_datetime, get_system_timezone

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# A rotated password keeps working this long, so every node can receive the new one.
PREVIOUS_PASSWORD_GRACE = timedelta(minutes=10)


class ControlClusterCredentials:
	"""Render the [auth] and [cluster] sections of one service's control daemon configuration.

	Atlas Settings holds `<service>_cluster_password`, `previous_<service>_cluster_password`,
	and `<service>_cluster_password_rotated_on` for each service."""

	def __init__(self, settings: AtlasSettings, service: str) -> None:
		self.settings = settings
		self.service = service

	@property
	def password(self) -> str:
		password = self.settings.get_password(f"{self.service}_cluster_password", raise_exception=False)
		if not password:
			frappe.throw(_("Atlas Settings holds no {0} cluster password.").format(self.service))
		return password

	@property
	def previous_password(self) -> str:
		return (
			self.settings.get_password(f"previous_{self.service}_cluster_password", raise_exception=False)
			or ""
		)

	@cached_property
	def password_hash(self) -> str:
		return bcrypt.hashpw(self.password.encode(), bcrypt.gensalt()).decode()

	@cached_property
	def previous_password_hash(self) -> str:
		if not self.previous_password:
			return ""
		return bcrypt.hashpw(self.previous_password.encode(), bcrypt.gensalt()).decode()

	@property
	def previous_password_valid_until(self) -> int:
		"""Return the Unix time when the previous password expires."""
		rotated_on = getattr(self.settings, f"{self.service}_cluster_password_rotated_on", None)
		if not self.previous_password or not rotated_on:
			return 0

		rotation = get_datetime(rotated_on).replace(tzinfo=ZoneInfo(get_system_timezone()))
		return int((rotation.astimezone(UTC) + PREVIOUS_PASSWORD_GRACE).timestamp())

	@property
	def digest_values(self) -> tuple[str, ...]:
		"""Return the values that change the rendered sections, for a configuration digest."""
		return (self.password, self.previous_password, self.settings.jwks_url, self.settings.issuer)

	def get_auth_section(self, audience_id: str) -> str:
		"""Return the [auth] section: the cluster password for Atlas, and JWTs for Central."""
		return "\n".join(
			[
				"[auth]",
				f'password_hash = "{self.password_hash}"',
				f'previous_password_hash = "{self.previous_password_hash}"',
				f"previous_password_valid_until = {self.previous_password_valid_until}",
				f"jwks_url = {json.dumps(self.settings.jwks_url)}",
				f"jwks_audience_id = {json.dumps(audience_id)}",
				f"jwks_issuers = {json.dumps(['central', self.settings.issuer])}",
			]
		)

	def get_cluster_section(self, node_id: str, peers: list[dict[str, str]]) -> str:
		"""Return the [cluster] section with every member's HTTPS address."""
		members = ", ".join(
			f"{{ node_id = {json.dumps(peer['node_id'])}, address = {json.dumps(peer['address'])} }}"
			for peer in peers
		)
		return "\n".join(
			[
				"[cluster]",
				f"node_id = {json.dumps(node_id)}",
				f"password = {json.dumps(self.password)}",
				f"previous_password = {json.dumps(self.previous_password)}",
				f"previous_password_valid_until = {self.previous_password_valid_until}",
				f"peers = [{members}]",
			]
		)
