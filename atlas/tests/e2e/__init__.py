"""End-to-end tests for Atlas.

Tests are grouped by **operator use case** (see [use_cases/](./use_cases/)).
Each use case module exercises one operator-visible operation: provisioning
a server, syncing an image, provisioning a VM, operating a VM, running an
ad-hoc task, talking to DigitalOcean, or using the SSH primitive directly.

`run_all()` is the cheap regression entry point: one shared droplet, every
use case that takes a server runs against it, droplet cleaned up at the
end. `run_all_coverage()` additionally runs the dedicated-droplet use cases
(server-provisioning fresh path, DigitalOcean-client round trip).

Use cases that bring their own droplet semantics (or no droplet at all) are
not orchestrated here — they are invoked directly:

    bench --site atlas.local execute atlas.tests.e2e.use_cases.ssh_primitive.run
    bench --site atlas.local execute atlas.tests.e2e.use_cases.digitalocean_client.run
    bench --site atlas.local execute atlas.tests.e2e.use_cases.server_provisioning.run
"""

import time
import traceback

from atlas.tests.e2e._shared import (
	cleanup_droplet,
	ensure_bootstrapped_server,
	get_client,
	sweep_old_droplets,
)
from atlas.tests.e2e.use_cases import (
	desk_buttons,
	image_sync,
	run_task,
	server_provisioning,
	ssh_primitive,
	virtual_machine_lifecycle,
	virtual_machine_provisioning,
)


def run_all() -> None:
	"""Run every use case that takes a Server against one shared droplet.

	The droplet is created once (or reused if an Active+reachable one already
	exists), every use case runs against it with `keep=True`, and the
	`finally` block deletes it when we provisioned it ourselves.

	Use cases not orchestrated here:

	- [ssh_primitive.run](./use_cases/ssh_primitive.py) — operator-provided
	  droplet path; no DO state.
	- [digitalocean_client.run](./use_cases/digitalocean_client.py) — owns
	  its own throwaway droplet.
	- [server_provisioning.run](./use_cases/server_provisioning.py) — owns
	  the fresh-provision flow; folding it in would either tear down the
	  shared droplet mid-run or dilute its contract.
	"""
	overall_start = time.monotonic()
	client = get_client()
	sweep_old_droplets(client)

	server, _client, created_now = ensure_bootstrapped_server(reuse=True, keep=True)

	use_cases = [
		("image-sync", image_sync.run),
		("vm-provisioning", virtual_machine_provisioning.run),
		("vm-lifecycle", virtual_machine_lifecycle.run),
		("run-task", run_task.run),
		("desk-buttons", desk_buttons.run),
		("server-provisioning (validation)", server_provisioning.run_against_shared),
		("ssh-primitive (transport+bootstrap)", ssh_primitive.run_against_shared),
	]

	results: list[tuple[str, str, float]] = []
	try:
		for label, runner in use_cases:
			use_case_start = time.monotonic()
			try:
				runner(reuse=True, keep=True)
				results.append((label, "OK", time.monotonic() - use_case_start))
			except Exception:
				results.append((label, "FAIL", time.monotonic() - use_case_start))
				traceback.print_exc()
				break
	finally:
		if created_now and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))

	total = time.monotonic() - overall_start
	print("")
	print("=" * 60)
	for label, outcome, seconds in results:
		print(f"{label:<40} {outcome} in {seconds:.0f}s")
	print(f"Total: {total:.0f}s. One droplet used{' + cleaned up' if created_now else ' (reused)'}.")
	print("=" * 60)

	failed = [label for label, outcome, _ in results if outcome != "OK"]
	if failed:
		raise AssertionError(f"failures: {', '.join(failed)}")


def run_all_coverage() -> None:
	"""Run everything that contributes to e2e coverage in a single bench call.

	Cost: three billable droplets. The shared droplet (used by the
	use cases orchestrated by `run_all`), the DigitalOcean-client round
	trip's throwaway, and the server-provisioning fresh-provision flow's
	droplet. Server-provisioning is the only path that hits
	`Server Provider.provision_server` and `finish_provisioning` against a
	fresh droplet, so it must run if those modules are to be covered.
	"""
	from atlas.tests.e2e.use_cases import digitalocean_client

	# digitalocean_client opens with a no-droplet smoke; run it first so a
	# transient DO outage fails fast before we burn an hour bootstrapping.
	print("--- digitalocean-client (smoke + round trip) ---")
	digitalocean_client.run()

	print("--- server-provisioning (fresh provision) ---")
	server_provisioning.run()

	run_all()
