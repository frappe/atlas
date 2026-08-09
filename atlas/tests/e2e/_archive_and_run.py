"""One-shot e2e driver: archive every existing Active Server (and delete its
droplet), then run the full `run_all` regression against a FRESH droplet.

This exists so the run() single-string `run()` cutover is proven end-to-end on a
real host — bootstrap-server.py, provision-vm.py, the lifecycle and image-sync
verbs all execute over real SSH with the new `{}`-quoting renderer. Archiving the
shared server first forces `ensure_bootstrapped_server` down its fresh-provision
path instead of reusing the old droplet.

Invoke:
    bench --site e2e.local execute atlas.tests.e2e._archive_and_run.archive_then_run_all
"""

import frappe

from atlas.tests.e2e import run_all
from atlas.tests.e2e._config import get_client
from atlas.tests.e2e._droplets import cleanup_droplet


def archive_existing_servers() -> None:
	"""Mark every Active/Broken Server Archived and delete its droplet, so the next
	`run_all` provisions a brand-new host. Best-effort on the droplet delete: an
	already-gone droplet is fine."""
	client = get_client()
	rows = frappe.get_all(
		"Server",
		filters={"status": ["in", ["Active", "Broken", "Bootstrapping", "Pending"]]},
		fields=["name", "status", "provider_resource_id", "ipv4_address"],
	)
	print(f"[archive] {len(rows)} server row(s) to archive")
	for row in rows:
		if row.provider_resource_id:
			print(
				f"[archive] deleting droplet {row.provider_resource_id} ({row.ipv4_address}) for {row.name}"
			)
			cleanup_droplet(client, int(row.provider_resource_id))
		frappe.db.set_value("Server", row.name, "status", "Archived")
		print(f"[archive] {row.name}: {row.status} -> Archived")
	frappe.db.commit()


def archive_then_run_all() -> None:
	archive_existing_servers()
	print("[archive] existing servers archived; running full e2e on a FRESH droplet")
	run_all()
