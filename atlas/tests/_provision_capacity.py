"""Operator one-shot: provision ONE real DigitalOcean server and KEEP it.

Drives the real provisioning API end-to-end so we have host capacity for the
UI tests:

  Provider.provision_server(title)   -> creates the droplet + Server row
  finish_provisioning(server_name)   -> polls DO, bootstraps over SSH, Active

`finish_provisioning` normally runs in the `long` worker queue. We run it
INLINE here so the result is deterministic and observable in one process
(no dependency on a running worker), and we NEVER tear the droplet down —
that's the "keep it running" part.

Run:
  bench --site atlas.tests.local execute atlas.tests._provision_capacity.run
"""

import time

import frappe

from atlas.atlas.providers.worker import finish_provisioning
from atlas.tests.e2e._shared import ensure_e2e_provider, get_client


def run():
	client = get_client()

	# Round-trip the token first so a dead/expired credential fails fast and
	# free, before we create anything billable.
	provider = ensure_e2e_provider()
	auth = provider.authenticate()
	print(f"[capacity] authenticate() -> {auth}")
	if not auth.get("ok"):
		frappe.throw(f"DO token did not authenticate: {auth.get('error')!r}")

	title = f"atlas-ui-capacity-{int(time.time())}"
	print(f"[capacity] provisioning {title!r} via Provider {provider.name!r} (region/size/image from DO Settings)")

	start = time.monotonic()
	server_name = provider.provision_server(title)
	server = frappe.get_doc("Server", server_name)
	print(
		f"[capacity] Server row created: name={server_name} "
		f"droplet={server.provider_resource_id!r} status={server.status!r}"
	)

	# Drive bootstrap synchronously (idempotent; sync-callable). This polls DO
	# until the droplet is ready, populates IPs, waits for SSH, runs
	# bootstrap-server.py, and flips the row to Active (or Broken).
	print("[capacity] running finish_provisioning inline (poll -> SSH -> bootstrap)...")
	finish_provisioning(server_name)

	server.reload()
	elapsed = int(time.monotonic() - start)
	print(f"[capacity] done in {elapsed}s")
	print(
		f"[capacity] RESULT name={server_name} title={title!r} status={server.status!r} "
		f"droplet={server.provider_resource_id!r} ipv4={server.ipv4_address!r} "
		f"ipv6={server.ipv6_address!r} firecracker={server.firecracker_version!r} "
		f"jailer={server.jailer_version!r} kernel={server.kernel_version!r}"
	)

	if server.status != "Active":
		frappe.throw(f"server ended {server.status!r}, expected Active — check the Task list")

	print("[capacity] Server is Active and KEPT running (no teardown). Capacity ready for UI tests.")
	return server_name
