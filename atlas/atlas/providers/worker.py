"""Provider worker — polling + post-provision bootstrap.

`finish_provisioning` is the background-job entrypoint. The provider
abstraction owns `describe()`; this module wraps it in a wait loop, then
drives the Server through `Bootstrapping → Active` (or `Broken`).
"""

from __future__ import annotations

import json
import time

import frappe

from atlas.atlas.providers.base import Provider, ProvisionResult

POLL_INTERVAL_SECONDS = 5
DEFAULT_READY_TIMEOUT = 600


def wait_until_ready(
	provider: Provider,
	identifier: str,
	timeout_seconds: int | None = None,
) -> ProvisionResult:
	"""Poll `provider.describe(identifier)` until `ready=True` or timeout.

	`timeout_seconds` defaults to the provider's own `ready_timeout_seconds`
	(droplets: seconds; Scaleway bare-metal installs: up to an hour). A
	`ProviderError` raised by `describe()` — a terminal vendor state — is *not*
	caught: it propagates so `finish_provisioning` marks the Server `Broken`
	immediately rather than spinning out the full timeout."""
	if timeout_seconds is None:
		timeout_seconds = getattr(provider, "ready_timeout_seconds", DEFAULT_READY_TIMEOUT)
	deadline = time.monotonic() + timeout_seconds
	while True:
		result = provider.describe(identifier)
		if result.ready:
			return result
		if time.monotonic() >= deadline:
			frappe.throw(f"provider resource {identifier!r} not ready after {timeout_seconds}s")
		time.sleep(POLL_INTERVAL_SECONDS)


def finish_provisioning(server_name: str) -> None:
	"""Background job: wait for the host to be ready, then bootstrap."""
	import atlas
	from atlas.atlas.ssh import connection_for_server, wait_for_ssh

	frappe.logger("atlas").info(f"finish_provisioning: start server={server_name}")
	server = frappe.get_doc("Server", server_name)
	provider = atlas.get_provider()

	# Self-Managed has no vendor-side resource id; the worker hands it the
	# Server's UUID so describe() can look the row up.
	identifier = server.provider_resource_id or server.name
	frappe.logger("atlas").info(f"finish_provisioning: waiting for provider resource {identifier!r}")

	# Wrap the whole ready-wait → SSH → bootstrap path: any terminal failure
	# (a ProviderError from describe(), a ready/SSH timeout, or a bootstrap
	# error) lands the Server in Broken instead of leaving it stuck Pending.
	try:
		result = wait_until_ready(provider, identifier)
		frappe.logger("atlas").info(
			f"finish_provisioning: ready ipv4={result.networking.ipv4_address if result.networking else None}"
		)

		_apply_describe_result(server, result)
		server.status = "Bootstrapping"
		server.save(ignore_permissions=True)
		frappe.db.commit()

		# Provider hook: a vendor whose image blocks root login (Scaleway) does
		# its one-shot 'first contact' here, before the root-SSH wait. Default
		# is a no-op (DO/Self-Managed expose root directly).
		frappe.logger("atlas").info("finish_provisioning: prepare_host")
		provider.prepare_host(server)

		# A Fake server (developer_mode) has no host to reach; skip the SSH wait
		# and go straight to bootstrap, whose Task is faked and still records the
		# host versions onto the row. Real providers wait for root SSH first.
		from atlas.atlas.providers.fake_tasks import is_fake_server

		if not is_fake_server(server.name):
			frappe.logger("atlas").info("finish_provisioning: waiting for SSH")
			wait_for_ssh(connection_for_server(server), timeout_seconds=300)
			frappe.logger("atlas").info("finish_provisioning: SSH reachable; running bootstrap script")

		server.bootstrap()
	except Exception as exception:
		frappe.logger("atlas").error(f"finish_provisioning: failed: {exception}")
		server.reload()
		server.status = "Broken"
		server.save(ignore_permissions=True)
		frappe.db.commit()
		raise

	server.reload()
	server.status = "Active"
	server.save(ignore_permissions=True)
	# nosemgrep: frappe-manual-commit -- background job: persist the final Active state so it is durable and observers see provisioning completed
	frappe.db.commit()
	frappe.logger("atlas").info(f"finish_provisioning: server {server_name} is Active")


def _apply_describe_result(server, result: ProvisionResult) -> None:
	if result.networking:
		server.ipv4_address = result.networking.ipv4_address
		server.ipv6_address = result.networking.ipv6_address
		server.ipv6_prefix = result.networking.ipv6_prefix
		server.ipv6_virtual_machine_range = result.networking.ipv6_virtual_machine_range
	if result.size:
		server.size = result.size
	if result.image:
		server.image = result.image
	if result.provider_metadata is not None:
		server.provider_metadata = json.dumps(result.provider_metadata)
