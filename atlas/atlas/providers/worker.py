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

# Reconciler grace windows: how long a Server may sit in a pre-Active status
# (with its `modified` untouched) before the sweep re-drives it. The worker
# bumps `modified` at each step, so keying off a STALE `modified` — not the
# status alone — leaves an actively-progressing bootstrap untouched while
# catching one whose finish_provisioning job was lost (never ran, left no
# trace). Pending should flip within seconds of the box reporting ready, so a
# short window is safe; Bootstrapping is legitimately long (Scaleway install +
# bootstrap-server.py) so it gets much more headroom.
RECONCILE_PENDING_GRACE_SECONDS = 15 * 60
RECONCILE_BOOTSTRAPPING_GRACE_SECONDS = 70 * 60


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

	# A host reaching Active joins the WireGuard host mesh (design §3, trigger 1):
	# every OTHER host needs the newcomer as a peer, and the newcomer needs all of
	# them, so this reconciles the WHOLE mesh. Enqueued after the Active commit above
	# so it runs against the committed state; a push failure is caught by the converging
	# reconcile + the scheduler backstop, never wedging finish_provisioning.
	from atlas.atlas.host_mesh import enqueue_reconcile_host_mesh

	enqueue_reconcile_host_mesh()


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


def finish_provisioning_job_id(server_name: str) -> str:
	"""The stable RQ job id used to deduplicate finish_provisioning enqueues for
	one Server. A lost job leaves no row in any registry, but a job that is
	genuinely still queued/running carries this id — so the reconciler (and the
	operator Recover button) can re-enqueue without ever stacking a second
	finish_provisioning on top of one still in flight."""
	return f"finish_provisioning::{server_name}"


def enqueue_finish_provisioning(server_name: str) -> bool:
	"""Enqueue finish_provisioning for one Server, deduplicated.

	Returns True if a job was enqueued, False if one was already queued/running
	(so the caller can report "already in progress"). Shared by the scheduled
	reconciler and the Server.recover() escape hatch — both want the same
	"re-drive unless already in flight" semantics. finish_provisioning itself is
	idempotent (re-running against a ready box just flips it Active), so a rare
	double-enqueue is harmless; dedup only keeps the queue clean."""
	from frappe.utils.background_jobs import is_job_enqueued

	if is_job_enqueued(finish_provisioning_job_id(server_name)):
		return False
	frappe.enqueue(
		"atlas.atlas.providers.worker.finish_provisioning",
		queue="long",
		timeout=1800,
		job_id=finish_provisioning_job_id(server_name),
		deduplicate=True,
		server_name=server_name,
	)
	return True


def reconcile_pending_servers() -> list[str]:
	"""Scheduled safety net: re-drive any Server stranded pre-Active.

	`provision()` creates the real, billing vendor box synchronously, then hands
	the rest of the lifecycle (describe → IPs → Bootstrapping → bootstrap →
	Active) to ONE fire-and-forget finish_provisioning job. If that job is lost —
	a forked work-horse that dies before the body, a worker restart that drops an
	un-acked job, a redis eviction, a TTL reap — the Server sits in Pending /
	Bootstrapping forever with a paid-for box behind it and nothing notices. This
	sweep is the missing recovery: it re-enqueues finish_provisioning (idempotent)
	for any such row, independent of WHY the original job was lost.

	Guards against interrupting healthy work:
	- only rows with a `provider_resource_id` (so describe() has something to poll —
	  a row that never got a vendor id failed earlier and earlier);
	- only rows whose `modified` is older than the per-status grace window (the
	  worker bumps `modified` at each step, so a live bootstrap is never touched);
	- dedup via enqueue_finish_provisioning (no second job atop one still running).

	Returns the Server names it re-enqueued (for logging / tests).
	"""
	now = frappe.utils.now_datetime()
	windows = {
		"Pending": RECONCILE_PENDING_GRACE_SECONDS,
		"Bootstrapping": RECONCILE_BOOTSTRAPPING_GRACE_SECONDS,
	}
	re_enqueued: list[str] = []
	for status, grace_seconds in windows.items():
		cutoff = frappe.utils.add_to_date(now, seconds=-grace_seconds)
		stale = frappe.get_all(
			"Server",
			filters={
				"status": status,
				"provider_resource_id": ("is", "set"),
				"modified": ("<", cutoff),
			},
			pluck="name",
		)
		for server_name in stale:
			if enqueue_finish_provisioning(server_name):
				re_enqueued.append(server_name)
				frappe.logger("atlas").warning(
					f"reconcile_pending_servers: re-enqueued finish_provisioning for "
					f"{server_name} (stuck {status} past {grace_seconds}s)"
				)
	return re_enqueued
