from __future__ import annotations

from datetime import UTC, datetime, timedelta

import frappe
from frappe.utils import validate_email_address

from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.service.core.warpgate.client import WarpgateClient, WarpgateError
from atlas.service.core.warpgate.sync import ALL_HOSTS_ROLE, get_host_role

ALL_HOSTS = "all"
SSO_PROVIDER = "central"
DEFAULT_MAXIMUM_GRANT_HOURS = 24
# People sign in through Central in the browser, and approve each SSH login there.
USER_CREDENTIAL_POLICY = {"http": ["Sso"], "ssh": ["WebUserApproval"]}


class WarpgateUnavailable(AtlasUserError):
	"""This region has no Warpgate, or Warpgate did not answer."""

	code = "warpgate_unavailable"
	http_status_code = 503


class HostNotFound(AtlasUserError):
	code = "host_not_found"
	http_status_code = 404


class HostAccess:
	"""Grant and revoke temporary Warpgate access to one host or to every host."""

	def __init__(self, host_id: str, client: WarpgateClient | None = None) -> None:
		self.role_name = self.get_role_name(host_id)
		self.client = client or WarpgateClient.from_settings()
		if self.client is None:
			raise WarpgateUnavailable("This region has no Warpgate.")

	@staticmethod
	def get_role_name(host_id: str) -> str:
		"""Return the Warpgate role for a Metal Server name, or all-hosts for `all`."""
		if host_id == ALL_HOSTS:
			return ALL_HOSTS_ROLE

		title = frappe.db.get_value("Metal Server", {"name": host_id, "status": ["!=", "Deleted"]}, "title")
		if not title:
			raise HostNotFound(f"Host {host_id} does not exist.")
		return get_host_role(title)

	def grant(self, email: str, expires_at: datetime) -> str:
		"""Open the host until expires_at and return the stored email. A repeated grant moves the end."""
		email = self.normalize_email(email)
		self.validate_expiry(expires_at)
		try:
			role_id = self.get_role_id()
			user_id = self.ensure_user(email)
			granted = {role["id"] for role in self.client.list_user_roles(user_id)}
			self.client.grant_user_role(user_id, role_id, expires_at, is_granted=role_id in granted)
		except WarpgateError as error:
			raise WarpgateUnavailable(str(error)) from error
		return email

	def revoke(self, email: str) -> None:
		"""Close the host now. A missing user or grant is not an error."""
		email = self.normalize_email(email)
		try:
			user = self.client.find_user(email)
			if user is None:
				return
			role_id = self.get_role_id()
			if any(role["id"] == role_id for role in self.client.list_user_roles(user["id"])):
				self.client.revoke_user_role(user["id"], role_id)
		except WarpgateError as error:
			raise WarpgateUnavailable(str(error)) from error

	def get_role_id(self) -> str:
		for role in self.client.list_roles():
			if role["name"] == self.role_name:
				return role["id"]
		raise AtlasConflictError(
			f"Warpgate has no role {self.role_name} yet. The target sync adds it within a minute."
		)

	def ensure_user(self, email: str) -> str:
		"""Register the user for Central sign-in. Each step repairs a registration that stopped halfway."""
		user = self.client.find_user(email) or self.client.create_user(email)
		policy = self.client.get_user(user["id"]).get("credential_policy") or {}
		if {protocol: policy.get(protocol) for protocol in USER_CREDENTIAL_POLICY} != USER_CREDENTIAL_POLICY:
			self.client.set_credential_policy(user, USER_CREDENTIAL_POLICY)
		credentials = self.client.list_sso_credentials(user["id"])
		if not any(item["provider"] == SSO_PROVIDER and item["email"] == email for item in credentials):
			self.client.add_sso_credential(user["id"], SSO_PROVIDER, email)
		return user["id"]

	@staticmethod
	def normalize_email(email: str) -> str:
		email = email.strip().lower()
		# The validator returns a display name's address, and a comma list as given.
		if "," in email or validate_email_address(email) != email:
			raise AtlasUserError("Enter a valid email address.")
		return email

	@staticmethod
	def validate_expiry(expires_at: datetime) -> None:
		if expires_at.tzinfo is None:
			raise AtlasUserError("Send expires_at with a time zone.")

		maximum_hours = frappe.conf.get("warpgate_grant_max_hours") or DEFAULT_MAXIMUM_GRANT_HOURS
		now = datetime.now(UTC)
		if not now < expires_at <= now + timedelta(hours=maximum_hours):
			raise AtlasUserError(f"expires_at must be in the future and at most {maximum_hours} hours away.")
