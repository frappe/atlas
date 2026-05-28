"""Self-Managed provider implementation.

Self-Managed blurs the abstraction a little: Atlas itself owns the
truth, not a vendor. We keep the five-method shape so the worker doesn't
branch — `describe()` reads the Server row, `provision()` echoes back
what the operator typed, `destroy()` is a no-op.
"""

from __future__ import annotations

import frappe

from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	Provider,
	ProvisionRequest,
	ProvisionResult,
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
			frappe.throw("Self-Managed provision requires prebuilt_networking")
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
