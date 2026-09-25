from __future__ import annotations

import ipaddress
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone
from frappe.utils.caching import site_cache

from atlas.atlas.core.tls.certificate import (
	CertificateError,
	create_certificate_authority,
	issue_certificate,
	read_certificate,
	verify_issued_certificate,
	verify_key_pair,
)

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

TLS_DIRECTORY = ("private", "atlas-metal-tls")
CERTIFICATE_RENEWAL_WINDOW_DAYS = 30
CERTIFICATE_AUTHORITY_WARNING_DAYS = 180
CLIENT_CERTIFICATE_CHECK_SECONDS = 3600


def ensure_certificate_authority(settings: "AtlasSettings") -> bool:
	"""Create the regional Metal certificate authority when it is absent."""
	certificate = settings.get_password("metal_tls_ca_certificate", raise_exception=False)
	private_key = settings.get_password("metal_tls_ca_private_key", raise_exception=False)
	if certificate and private_key:
		try:
			verify_key_pair(certificate, private_key)
		except CertificateError as error:
			frappe.throw(str(error))
		return False
	if certificate or private_key:
		frappe.throw(_("Set both Metal TLS certificate authority fields or clear both fields."))
	if not settings.region_name or not settings.wildcard_domain:
		return False

	identity = f"Atlas Metal CA {settings.region_name} {settings.wildcard_domain}"
	settings.metal_tls_ca_certificate, settings.metal_tls_ca_private_key = create_certificate_authority(
		identity
	)
	return True


def ensure_server_certificate(server: "MetalServer") -> tuple[str, str, str]:
	"""Return the CA and a current certificate for one Metal server."""
	settings = server.settings
	if ensure_certificate_authority(settings):
		settings.save(ignore_permissions=True, ignore_version=True)
	ca_certificate = settings.get_password("metal_tls_ca_certificate", raise_exception=False)
	ca_private_key = settings.get_password("metal_tls_ca_private_key", raise_exception=False)
	if not ca_certificate or not ca_private_key:
		frappe.throw(_("Atlas Settings has no Metal TLS certificate authority."))

	addresses = _server_addresses(server)
	identity = f"{server.name}.{settings.wildcard_domain}"
	certificate = server.get_password("metald_tls_certificate", raise_exception=False)
	private_key = server.get_password("metald_tls_private_key", raise_exception=False)
	if not _certificate_matches(certificate, private_key, ca_certificate, identity, addresses):
		certificate, private_key = issue_certificate(ca_certificate, ca_private_key, identity, addresses)
		server.metald_tls_certificate = certificate
		server.metald_tls_private_key = private_key
		server.metald_tls_expires_on = convert_utc_to_system_timezone(
			read_certificate(certificate).expires_on
		).replace(tzinfo=None)
		server.save(ignore_permissions=True, ignore_version=True)
	return ca_certificate, certificate, private_key


def is_certificate_authority_expiring() -> bool:
	"""Return whether the regional authority expires inside its warning window."""
	certificate = frappe.get_single("Atlas Settings").get_password(
		"metal_tls_ca_certificate", raise_exception=False
	)
	if not certificate:
		return False
	return read_certificate(certificate).expires_on <= datetime.now(UTC) + timedelta(
		days=CERTIFICATE_AUTHORITY_WARNING_DAYS
	)


def ensure_atlas_client_certificate(settings: "AtlasSettings") -> bool:
	"""Issue the Atlas client certificate when it is absent or expires soon."""
	ca_certificate = settings.get_password("metal_tls_ca_certificate", raise_exception=False)
	ca_private_key = settings.get_password("metal_tls_ca_private_key", raise_exception=False)
	if not ca_certificate or not ca_private_key:
		return False

	identity = atlas_client_identity(settings)
	certificate = settings.get_password("atlas_tls_certificate", raise_exception=False)
	private_key = settings.get_password("atlas_tls_private_key", raise_exception=False)
	if _certificate_matches(certificate, private_key, ca_certificate, identity, []):
		return False

	settings.atlas_tls_certificate, settings.atlas_tls_private_key = issue_certificate(
		ca_certificate, ca_private_key, identity, []
	)
	return True


def atlas_client_identity(settings: "AtlasSettings") -> str:
	"""Return the common name that Metal accepts on the Atlas API."""
	return f"atlas.{settings.wildcard_domain}"


def ca_file() -> str:
	"""Write the current regional CA to a private site file and return its path."""
	certificate = frappe.get_single("Atlas Settings").get_password(
		"metal_tls_ca_certificate", raise_exception=False
	)
	if not certificate:
		frappe.throw(_("Atlas Settings has no Metal TLS certificate authority."))
	return _write_private_file("ca.crt", certificate)


def client_certificate_files() -> tuple[str, str]:
	"""Return the Atlas client pair files for a Metal request.

	The renewal check verifies the key pair, which costs tens of milliseconds, so it
	runs once per CLIENT_CERTIFICATE_CHECK_SECONDS or after Atlas Settings changes.
	"""
	return _client_certificate_files(str(frappe.get_cached_doc("Atlas Settings").modified))


@site_cache(ttl=CLIENT_CERTIFICATE_CHECK_SECONDS, maxsize=4)
def _client_certificate_files(_settings_modified: str) -> tuple[str, str]:
	settings = frappe.get_single("Atlas Settings")
	if ensure_atlas_client_certificate(settings):
		settings.save(ignore_permissions=True, ignore_version=True)
	certificate = settings.get_password("atlas_tls_certificate", raise_exception=False)
	private_key = settings.get_password("atlas_tls_private_key", raise_exception=False)
	if not certificate or not private_key:
		frappe.throw(_("Atlas Settings has no Atlas client certificate."))

	return _write_private_file("atlas.crt", certificate), _write_private_file("atlas.key", private_key)


def _write_private_file(name: str, content: str) -> str:
	directory = Path(frappe.get_site_path(*TLS_DIRECTORY))
	path = directory / name
	if path.is_file() and path.read_text() == content:
		return str(path)

	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=f"{name}.", delete=False) as temporary:
		temporary.write(content)
	temporary_path = temporary.name

	try:
		os.chmod(temporary_path, 0o600)
		os.replace(temporary_path, path)
	except OSError:
		Path(temporary_path).unlink(missing_ok=True)
		raise
	return str(path)


def _server_addresses(server: "MetalServer") -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
	values = (
		server.wireguard_ip_address,
		server.private_ipv4_address,
		server.public_ipv4_address,
	)
	if not all(values):
		frappe.throw(f"Metal Server {server.name} needs WireGuard, private, and public IP addresses.")
	try:
		return [ipaddress.ip_address(value) for value in values]
	except ValueError as error:
		frappe.throw(f"Metal Server {server.name} has an invalid TLS IP address: {error}")


def _certificate_matches(
	certificate: str | None,
	private_key: str | None,
	ca_certificate: str,
	identity: str,
	addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
	if not certificate or not private_key:
		return False
	try:
		verify_key_pair(certificate, private_key)
		verify_issued_certificate(certificate, ca_certificate)
		details = read_certificate(certificate)
	except CertificateError:
		return False
	return (
		details.dns_names == (identity,)
		and set(details.ip_addresses) == set(addresses)
		and details.expires_on > datetime.now(UTC) + timedelta(days=CERTIFICATE_RENEWAL_WINDOW_DAYS)
	)
