from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import frappe

from atlas.metal_server.core.atlas_peer import write_private_file
from atlas.service.core.warpgate.client import WarpgateClient, WarpgateError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# Only install-warpgate.py writes it, in the Atlas VM.
NGINX_CONFIG = Path("/etc/nginx/conf.d/warpgate.conf")
TOKEN_LIFETIME = timedelta(days=365)
RENEWAL_WINDOW = timedelta(days=30)


def is_warpgate_ui_installed() -> bool:
	return NGINX_CONFIG.exists()


def publish_warpgate_ui(settings: AtlasSettings) -> None:
	"""Point warpgate.<wildcard> at the Atlas site and serve the UI with the region certificate."""
	if not settings.warpgate_url or not is_warpgate_ui_installed():
		return

	settings.dns_provider_controller.upsert_cname_record(
		f"warpgate.{settings.wildcard_domain}", frappe.local.site
	)
	install_warpgate_certificate(settings)


def install_warpgate_certificate(settings: AtlasSettings) -> None:
	"""Write the wildcard certificate where the nginx server reads it, and reload nginx."""
	certificate = settings.get_password("wildcard_tls_certificate", raise_exception=False)
	key = settings.get_password("wildcard_tls_private_key", raise_exception=False)
	if not certificate or not key or not is_warpgate_ui_installed():
		return

	directory = Path(frappe.get_site_path("private", "warpgate"))
	write_private_file(directory / "tls.crt", certificate)
	write_private_file(directory / "tls.key", key)
	# Pilot grants the bench user these two commands for its own sites.
	for command in (["nginx", "-t"], ["systemctl", "reload", "nginx"]):
		subprocess.run(["sudo", "-n", *command], check=True, capture_output=True, timeout=60)


class WarpgateTokenManager:
	"""Keep one working Atlas token in Warpgate and in Atlas Settings. An expired token needs a setup rerun."""

	def __init__(self, settings: AtlasSettings | None = None) -> None:
		self.settings = settings or frappe.get_single("Atlas Settings")

	def renew(self) -> None:
		client = WarpgateClient.from_settings(self.settings)
		if client is None:
			return

		try:
			tokens = {token["id"]: token for token in client.list_api_tokens()}
		except WarpgateError as error:
			raise WarpgateError(
				"The Atlas Warpgate token no longer works. Run atlas-vm setup again to issue a new one."
			) from error
		if self.settings.warpgate_api_token_id not in tokens:
			raise WarpgateError("Warpgate API Token ID does not match a token. Run atlas-vm setup again.")

		now = datetime.now(UTC)
		expiry = datetime.fromisoformat(tokens[self.settings.warpgate_api_token_id]["expiry"])
		if expiry <= now + RENEWAL_WINDOW:
			created = client.create_api_token(now + TOKEN_LIFETIME)
			self.settings.warpgate_api_token = created["secret"]
			self.settings.warpgate_api_token_id = created["token"]["id"]
			self.settings.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
		self.delete_other_tokens()

	def delete_other_tokens(self) -> None:
		"""Delete every atlas token except the stored one, such as one a stopped renewal left."""
		client = WarpgateClient.from_settings(self.settings)
		if client is None:
			return
		if not self.settings.warpgate_api_token_id:
			raise WarpgateError(
				"Set Warpgate API Token ID. Without it, Atlas would delete the token it uses."
			)

		for token in client.list_api_tokens():
			if token["id"] != self.settings.warpgate_api_token_id:
				client.delete_api_token(token["id"])


def renew_warpgate_token() -> None:
	"""Scheduler entry point."""
	WarpgateTokenManager().renew()
