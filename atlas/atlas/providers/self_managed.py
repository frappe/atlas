"""Self-Managed provider implementation.

Self-Managed blurs the abstraction a little: Atlas itself owns the
truth, not a vendor. We keep the five-method shape so the worker doesn't
branch — `describe()` reads the Server row, `provision()` echoes back
what the operator typed, `destroy()` is a no-op.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	Provider,
	ProvisionRequest,
	ProvisionResult,
	ReservedIp,
	ServerNetworking,
)


@register
class SelfManagedProvider(Provider):
	provider_type = "Self-Managed"

	def authenticate(self) -> AuthResult:
		return AuthResult(ok=True, account_label="local")

	def discover(self) -> Capabilities:
		return Capabilities(sizes=(), images=())

	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		if request.prebuilt_networking is None:
			frappe.throw(_("Self-Managed provision requires prebuilt_networking"))
		return ProvisionResult(
			provider_resource_id="",
			size="",
			image="",
			ready=True,
			networking=request.prebuilt_networking,
			provider_metadata=None,
		)

	def describe(self, provider_resource_id: str) -> ProvisionResult:
		"""Read the Server row whose name matches `provider_resource_id`.

		The worker passes `server.name` (a UUID) here for Self-Managed
		because there is no vendor-side resource id to look up. The row
		already holds the networking data the operator entered; describe
		just packages it as a `ProvisionResult` for the caller.
		"""
		server = frappe.get_doc("Server", provider_resource_id)
		networking = ServerNetworking(
			ipv4_address=server.ipv4_address,
			ipv6_address=server.ipv6_address,
			ipv6_prefix=server.ipv6_prefix,
			ipv6_virtual_machine_range=server.ipv6_virtual_machine_range,
		)
		return ProvisionResult(
			provider_resource_id="",
			size="",
			image="",
			ready=True,
			networking=networking,
			provider_metadata=None,
		)

	def destroy(self, provider_resource_id: str) -> None:
		# Self-Managed has nothing remote to release. The operator decides
		# what to do with the physical host; Atlas just stops talking to it.
		return None

	def list_servers(self) -> tuple:
		# There is no vendor to ask. Adoption of a self-managed box stays the
		# manual Provision Server dialog where the operator types the IPs — the
		# only Self-Managed path. Discovery has nothing to enumerate.
		return ()

	# --- Reserved IPs ----------------------------------------------------
	# There is no vendor reserved-IP API. The operator owns a routable v4 and
	# routes it to the guest themselves; the `Reserved IP` row is created by
	# hand (operator-supplied `ip_address`, empty `provider_resource_id`). So
	# allocate has nothing to call and refuses; the rest are no-ops because
	# there is no vendor object to assign, list, or release.

	def allocate_reserved_ip(self) -> ReservedIp:
		frappe.throw(
			_(
				"Self-Managed has no reserved-IP API; create the Reserved IP row "
				"with an operator-supplied address instead of allocating one"
			)
		)

	def assign_reserved_ip(self, provider_resource_id: str, droplet_resource_id: str) -> None:
		return None

	def unassign_reserved_ip(self, provider_resource_id: str) -> None:
		return None

	def list_reserved_ips(self) -> tuple[ReservedIp, ...]:
		return ()

	def release_reserved_ip(self, provider_resource_id: str) -> None:
		return None
