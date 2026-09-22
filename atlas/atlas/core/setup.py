from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from math import isfinite
from types import MappingProxyType
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, now_datetime

from atlas.atlas.doctype.atlas_settings.atlas_settings import WILDCARD_TLS_RENEWAL_WINDOW_DAYS
from atlas.auth.jwks import sync_central_jwks
from atlas.metal_server.core.catalog_sync import CatalogSynchronizer

# The setup input carries the fields of the selected server provider only.
PROVIDER_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
	{
		"Generic": (),
		"Scaleway": (
			"scaleway_organization_id",
			"scaleway_project_id",
			"scaleway_zone",
			"scaleway_machine_billing_cycle",
			"scaleway_access_key",
			"scaleway_secret_key",
		),
		"AWS": (
			"aws_region",
			"aws_availability_zone",
			"aws_access_key_id",
			"aws_secret_access_key",
		),
	}
)

# A provider resource carries these values, so a completed region cannot change them.
PROVIDER_IMMUTABLE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
	{
		"Generic": (),
		"Scaleway": ("scaleway_organization_id", "scaleway_project_id", "scaleway_zone"),
		"AWS": ("aws_region", "aws_availability_zone"),
	}
)

OPTIONAL_STRING_FIELDS = frozenset(
	{"central_jwks_url", "default_metal_machine_size", "default_metal_machine_image"}
)


@dataclass(frozen=True, slots=True)
class AtlasSetupConfiguration:
	"""Store the operator values for one Atlas region."""

	server_provider: str
	dns_provider: str
	region_name: str
	region_id: int
	wildcard_domain: str
	private_network_cidr: str
	private_network_mtu: int
	central_jwks_url: str
	public_ssh_key: str
	route53_access_key_id: str
	route53_access_key_secret: str
	letsencrypt_email: str
	is_letsencrypt_staging: bool
	is_wildcard_tls_auto_renew_enabled: bool
	use_dedicated_sleepy_vm_hosts: bool
	placement_strategy: str
	sleepy_vm_overcommit_factor: float
	auto_spawn_metal_server: bool
	default_metal_machine_size: str
	default_metal_machine_image: str
	scaleway_organization_id: str = ""
	scaleway_project_id: str = ""
	scaleway_zone: str = ""
	scaleway_machine_billing_cycle: str = ""
	scaleway_access_key: str = ""
	scaleway_secret_key: str = ""
	aws_region: str = ""
	aws_availability_zone: str = ""
	aws_access_key_id: str = ""
	aws_secret_access_key: str = ""

	@classmethod
	def from_dict(cls, values: Any) -> "AtlasSetupConfiguration":
		"""Create a configuration from the deployment command input."""
		if not isinstance(values, dict):
			raise ValueError("Atlas setup configuration must be a JSON object")

		provider = values.get("server_provider")
		if provider not in PROVIDER_FIELDS:
			raise ValueError(
				f"Atlas setup field server_provider must be one of {', '.join(sorted(PROVIDER_FIELDS))}"
			)

		expected_fields = cls.expected_fields(provider)
		if set(values) != expected_fields:
			missing = sorted(expected_fields - set(values))
			unknown = sorted(set(values) - expected_fields)
			parts = []
			if missing:
				parts.append(f"missing fields: {', '.join(missing)}")
			if unknown:
				parts.append(f"unknown fields: {', '.join(unknown)}")
			raise ValueError("Invalid Atlas setup configuration; " + "; ".join(parts))

		string_fields = expected_fields - {
			"region_id",
			"private_network_mtu",
			"sleepy_vm_overcommit_factor",
			"use_dedicated_sleepy_vm_hosts",
			"auto_spawn_metal_server",
			"is_letsencrypt_staging",
			"is_wildcard_tls_auto_renew_enabled",
		}
		for field in string_fields:
			value = values[field]
			if not isinstance(value, str) or (field not in OPTIONAL_STRING_FIELDS and not value.strip()):
				raise ValueError(f"Atlas setup field {field} must be a string")
		for field in ("region_id", "private_network_mtu"):
			value = values[field]
			if not isinstance(value, int) or isinstance(value, bool):
				raise ValueError(f"Atlas setup field {field} must be an integer")
		if not 0 <= values["region_id"] <= 65_535:
			raise ValueError("Atlas setup field region_id must be from 0 through 65535")
		if values["private_network_mtu"] <= 0:
			raise ValueError("Atlas setup field private_network_mtu must be positive")
		for field in (
			"use_dedicated_sleepy_vm_hosts",
			"auto_spawn_metal_server",
			"is_letsencrypt_staging",
			"is_wildcard_tls_auto_renew_enabled",
		):
			if not isinstance(values[field], bool):
				raise ValueError(f"Atlas setup field {field} must be true or false")
		factor = values["sleepy_vm_overcommit_factor"]
		if (
			not isinstance(factor, (int, float))
			or isinstance(factor, bool)
			or not isfinite(factor)
			or factor < 1
		):
			raise ValueError(
				"Atlas setup field sleepy_vm_overcommit_factor must be a finite number of at least 1"
			)
		if values["auto_spawn_metal_server"]:
			for field in ("default_metal_machine_size", "default_metal_machine_image"):
				if not values[field].strip():
					raise ValueError(
						f"Atlas setup field {field} is required when auto_spawn_metal_server is true"
					)

		normalized_values = {
			**values,
			"region_name": values["region_name"].strip().lower(),
			"sleepy_vm_overcommit_factor": float(factor),
		}
		return cls(**normalized_values)

	def settings_values(self) -> dict[str, object]:
		"""Return the values that belong to Atlas Settings."""
		return {field: getattr(self, field) for field in self.expected_fields(self.server_provider)}

	@classmethod
	def expected_fields(cls, provider: str) -> set[str]:
		"""Return the field names that one server provider needs."""
		provider_specific = set().union(*PROVIDER_FIELDS.values())
		common = {field.name for field in fields(cls)} - provider_specific
		return common | set(PROVIDER_FIELDS[provider])


class AtlasSetup:
	"""Reconcile one Atlas region from operator configuration."""

	SERVER_IMMUTABLE_FIELDS = (
		"server_provider",
		"region_name",
		"region_id",
		"private_network_cidr",
		"public_ssh_key",
	)
	DNS_IMMUTABLE_FIELDS = ("dns_provider", "wildcard_domain")

	def __init__(self, configuration: AtlasSetupConfiguration) -> None:
		self.configuration = configuration
		self.settings = frappe.get_single("Atlas Settings")

	def run(self) -> None:
		"""Apply settings and complete each setup phase."""
		self._validate_immutable_values()
		self._clear_certificate_for_environment_change()
		self.settings.update(self.configuration.settings_values())
		self.settings.save(ignore_permissions=True)

		self._setup_server_provider()
		self._setup_dns_provider()
		self._sync_catalogs()
		self._sync_central_keys()
		self._issue_wildcard_certificate()
		self._validate_result()
		# A site command has no request transaction. Keep the completed reconciliation.
		frappe.db.commit()  # nosemgrep

	def _validate_immutable_values(self) -> None:
		if self.settings.is_server_provider_setup_completed:
			self._validate_fields(self.SERVER_IMMUTABLE_FIELDS)
			self._validate_fields(PROVIDER_IMMUTABLE_FIELDS[self.configuration.server_provider])
		if self.settings.is_dns_setup_completed:
			self._validate_fields(self.DNS_IMMUTABLE_FIELDS)

	def _validate_fields(self, fields: tuple[str, ...]) -> None:
		for field in fields:
			current = self.settings.get(field)
			configured = getattr(self.configuration, field)
			if current != configured:
				frappe.throw(
					_("Atlas setup cannot change {0} from {1!r} to {2!r} after provider setup.").format(
						field, current, configured
					)
				)

	def _clear_certificate_for_environment_change(self) -> None:
		if bool(self.settings.is_letsencrypt_staging) == self.configuration.is_letsencrypt_staging:
			return
		if not self.settings.wildcard_tls_certificate:
			return

		self.settings.wildcard_tls_certificate = None
		self.settings.wildcard_tls_private_key = None

	def _setup_server_provider(self) -> None:
		if self.settings.is_server_provider_setup_completed:
			return

		try:
			self.settings.server_provider_controller.setup_infrastructure()
		finally:
			# Keep the provider resource IDs whether this phase or a later one fails.
			frappe.db.commit()  # nosemgrep

	def _setup_dns_provider(self) -> None:
		provider = self.settings.dns_provider_controller
		zone_id = provider.find_public_zone_id(self.settings.wildcard_domain)
		if not zone_id:
			frappe.throw(
				_("Route53 needs an existing public hosted zone for {0}.").format(
					self.settings.wildcard_domain
				)
			)
		if self.settings.route53_dns_zone_id and self.settings.route53_dns_zone_id != zone_id:
			frappe.throw(
				_("Route53 public zone {0} does not match stored zone {1}.").format(
					zone_id, self.settings.route53_dns_zone_id
				)
			)

		if self.settings.is_dns_setup_completed:
			return

		self.settings.route53_dns_zone_id = zone_id
		try:
			provider.bootstrap()
		finally:
			# Keep the zone ID and records whether this phase or a later one fails.
			frappe.db.commit()  # nosemgrep

	def _sync_catalogs(self) -> None:
		catalog = CatalogSynchronizer(self.settings.server_provider_controller)
		catalog.sync_server_sizes()
		catalog.sync_server_images()

	def _sync_central_keys(self) -> None:
		if not sync_central_jwks():
			frappe.throw(_("Atlas could not get the configured Central JSON Web Key Set."))
		self.settings.reload()

	def _issue_wildcard_certificate(self) -> None:
		if not self._does_certificate_need_renewal():
			return

		self.settings.issue_wildcard_certificate()
		# Keep the issued certificate because the ACME request cannot roll back.
		frappe.db.commit()  # nosemgrep

	def _does_certificate_need_renewal(self) -> bool:
		expires_on = self.settings.wildcard_tls_expires_on
		if not expires_on:
			return True

		return get_datetime(expires_on) <= add_days(now_datetime(), WILDCARD_TLS_RENEWAL_WINDOW_DAYS)

	def _validate_result(self) -> None:
		self.settings.reload()
		if not self.settings.is_setup_completed:
			frappe.throw(_("Atlas Settings setup is not complete."))
		if not self.settings.wildcard_tls_expires_on:
			frappe.throw(_("Atlas Settings has no wildcard TLS certificate."))
		if not frappe.db.count("Metal Server Size"):
			frappe.throw(_("Atlas has no Metal Server Size."))
		if not frappe.db.count("Metal Server Image"):
			frappe.throw(_("Atlas has no Metal Server Image."))
