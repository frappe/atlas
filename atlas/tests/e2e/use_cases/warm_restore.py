"""Warm snapshot fan-out — bake one warm golden, restore N distinct clones.

The use case behind `Image Build` → **Warm bake** (spec/15) and the warm path
of self-serve provisioning (spec/14): a pre-warmed golden bench VM is captured
(memory + disk at one paused instant) and fanned out into clones that RESUME
it in low seconds instead of cold-booting. Host facts only — every step here
is something just a real droplet can prove:

  1. warm bake: build VM → warm.sh (production stack up + pre-warm + freshen
     unit) → warm-snapshot-vm.py capture → `Warm` snapshot row.
  2. fan-out: two warm clones from the one golden. Both must reach Running,
     adopt their OWN identity (different hostname / machine-id / SSH host key,
     `/etc/atlas-vm-uuid` == each clone's uuid) while sharing ONE boot_id —
     the smoking gun of a memory restore: a cold boot mints a fresh boot_id,
     so two clones could never share one.
  3. warm serving: each clone answers HTTP 200 for the baked `site.local`
     straight from the resumed RAM — before any deploy step.
  4. deploy-on-warm: the real per-site deploy (--warm-vm-uuid freshen gate +
     RENAME of the baked site.local to the FQDN + `bench setup nginx` + reload,
     no `set-admin-password`, no restart) on one clone, proven by an HTTP 200
     for the FQDN Host header (served by the renamed vhost's `server_name
     <fqdn>`; the multitenant gunicorn resolves it by Host per request).
  5. cold-boot fallback: tamper the captured host signature → the next clone
     MUST cold-boot (fresh boot_id) yet still adopt its identity (the
     launcher's --metadata path) and serve.

Heavy (a full bench bake on first run, ~20-30 min) and therefore invoked
directly, not folded into run_all_smoke — same policy as self_serve_site:

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.warm_restore.run_smoke

Reuse: the bake is skipped when the shared server already carries an
Available Warm snapshot (a previous run's), so re-runs are minutes, not tens
of minutes. The fan-out clones are terminated in a `finally`; the golden and
the build VM survive as the durable artifacts they are.
"""

import time
from unittest.mock import patch

import frappe

from atlas.tests.e2e._config import control_plane_public_key, ephemeral_private_key, ephemeral_public_key
from atlas.tests.e2e._droplets import ensure_e2e_provider, phase
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e._tasks import wait_for_vm_running

# The fan-out width. Two is the smallest number that can prove distinctness.
CLONES = 2

# A made-up FQDN: the readiness probe sends it as the Host header against the
# clone's /128 directly, so no DNS is involved (Contract A is about the string).
DEPLOY_FQDN = "warm-e2e.atlas.invalid"

# Full-speed clones: the e2e measures the warm path, not the Shared-tier cgroup
# throttle (0.25 core makes even a resumed guest crawl — vm-boot-anatomy).
CLONE_CPU_CAP = 2.0

BAKE_TIMEOUT_SECONDS = 2700


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	with phase("warm-restore", reuse=reuse, keep=keep) as server:
		# Unit tests may have clobbered Atlas/DigitalOcean Settings with fakes;
		# re-seed from site config before anything SSHes (real-provision-traps).
		ensure_e2e_provider()
		# The durable host scripts (vm-restore.py + the atlas package, where the
		# signature guard and MMDS staging live) refresh via scp, not per-Task.
		uploaded = server.sync_scripts()
		print(f"[warm] sync_scripts: {uploaded} durable files refreshed on {server.name}")
		image = ensure_image_on_server(server.name)

		snapshot = _resolve_or_bake_warm(server, image.name)
		print(f"[warm] golden: {snapshot.name} kind={snapshot.kind} mem={snapshot.memory_bytes}B")

		clones: list[str] = []
		try:
			facts = _fan_out_and_verify(server, snapshot, clones)
			_verify_deploy_on_warm(clones[0])
			_verify_cold_fallback(server, snapshot, clones, warm_facts=facts)
		finally:
			for name in clones:
				_terminate_quietly(name)


def _resolve_or_bake_warm(server, base_image: str) -> "frappe.model.document.Document":
	"""The server's current warm golden, baking one via Image Build when absent.

	The bake is driven INLINE (run() in this process) for deterministic logs —
	the build VM's own provision still rides the background worker, exactly as
	in production. after_insert's enqueue is suppressed so the worker can't
	race this process into a duplicate bake."""
	from atlas.atlas.doctype.image_build import image_build as module
	from atlas.atlas.doctype.image_build.image_build import ImageBuild
	from atlas.atlas.placement import warm_bench_snapshot_for_server

	existing = warm_bench_snapshot_for_server(server.name)
	if existing:
		print(f"[warm] reusing Available warm snapshot {existing}")
		return frappe.get_doc("Virtual Machine Snapshot", existing)

	# The golden's authorized_keys (and every clone's MMDS identity) carry both
	# the controller's fleet key (deploy_site / warm.sh ride connection_for_guest)
	# and the e2e ephemeral key (the host-side facts probe) — one key per line,
	# the proxy-VM dual-key pattern.
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_public_key",
		ephemeral_public_key() + "\n" + control_plane_public_key(),
	)
	frappe.db.commit()

	print("[warm] no warm snapshot on this server; baking one (Image Build, warm=1)")
	with patch.object(ImageBuild, "after_insert", lambda self: None):
		build = frappe.get_doc(
			{
				"doctype": "Image Build",
				"recipe": "bench",
				"server": server.name,
				# Explicit: unit-test fixtures can leave several active image rows
				# on this site, which makes placement.default_image() ambiguous.
				"base_image": base_image,
				"warm": 1,
				"auto_register": 1,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	started = time.monotonic()
	module.run(build.name)
	build.reload()
	assert build.status == "Available", f"warm bake ended {build.status}: {build.error}"
	print(f"[warm] bake OK in {time.monotonic() - started:.0f}s; snapshot {build.snapshot}")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", build.snapshot)
	assert snapshot.kind == "Warm", f"bake produced kind={snapshot.kind}"
	assert snapshot.memory_directory, "warm snapshot has no memory_directory"
	registered = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	assert registered == snapshot.name, "auto_register did not wire the warm golden"
	return snapshot


def _fan_out_and_verify(server, snapshot, clones: list[str]) -> dict[str, dict]:
	"""Clone the golden CLONES times, then prove restore + distinct identity +
	warm serving for every clone. Returns the per-clone guest facts."""
	from atlas.atlas.deploy_site import wait_for_http

	started = time.monotonic()
	for index in range(CLONES):
		clones.append(
			snapshot.clone_to_new_vm(
				title=f"warm-clone-{index}",
				ssh_public_key=ephemeral_public_key() + "\n" + control_plane_public_key(),
				cpu_max_cores=CLONE_CPU_CAP,
			)
		)
	frappe.db.commit()
	for name in clones:
		vm = wait_for_vm_running(name, timeout_seconds=300)
		assert vm.warm_snapshot == snapshot.name
	print(f"[warm] {CLONES} clones Running in {time.monotonic() - started:.0f}s (insert→Running)")

	facts = {name: _guest_facts(server.name, name) for name in clones}
	for name in clones:
		# The freshen completed for exactly this VM, with the inject_identity
		# derivation rules (uuid-derived hostname + machine-id, fresh host key).
		assert facts[name]["FACT_ATLAS_VM_UUID"] == name, facts[name]
		assert facts[name]["FACT_HOSTNAME"] == f"atlas-{name[:8]}", facts[name]
		assert facts[name]["FACT_MACHINE_ID"] == name.replace("-", "")[:32], facts[name]

	first, second = clones[0], clones[1]
	# Distinctness: the freshen is per-clone, so nothing identity-bearing may
	# be shared (the Firecracker fan-out hazard this feature must defuse).
	assert facts[first]["FACT_MACHINE_ID"] != facts[second]["FACT_MACHINE_ID"]
	assert facts[first]["FACT_HOST_KEY"] != facts[second]["FACT_HOST_KEY"]
	assert facts[first]["FACT_HOSTNAME"] != facts[second]["FACT_HOSTNAME"]
	# The warm proof: both clones woke from the SAME frozen instant. Cold boots
	# mint fresh boot_ids and could never collide.
	assert facts[first]["FACT_BOOT_ID"] == facts[second]["FACT_BOOT_ID"], (
		f"boot_ids differ — the clones BOOTED instead of restoring: {facts}"
	)

	# Warm serving: the resumed stack answers for the baked site with no deploy
	# step at all — this is the latency win the feature exists for.
	for name in clones:
		vm = frappe.get_doc("Virtual Machine", name)
		t0 = time.monotonic()
		wait_for_http(vm.ipv6_address, "site.local", timeout_seconds=180)
		print(f"[warm] {name} serves site.local (HTTP 200) +{time.monotonic() - t0:.1f}s after facts probe")
	print(f"[warm] fan-out verified in {time.monotonic() - started:.0f}s total")
	return facts


def _verify_deploy_on_warm(clone_name: str) -> None:
	"""The real per-site deploy on a warm clone: the --warm-vm-uuid freshen gate +
	the RENAME of the baked site.local to the FQDN + `bench setup nginx`
	(`server_name <fqdn>` + v6 listener) + reload — NO `set-admin-password`, NO
	`setup production`, NO restart (the warm stack's multitenant gunicorn resolves
	the site by Host header per request, so the rename + reload serve the FQDN live).
	Proven end to end by an HTTP 200 for the FQDN Host header over the clone's /128."""
	from atlas.atlas.deploy_site import deploy_site, wait_for_http

	started = time.monotonic()
	deploy_site(clone_name, DEPLOY_FQDN)
	vm = frappe.get_doc("Virtual Machine", clone_name)
	wait_for_http(vm.ipv6_address, DEPLOY_FQDN, timeout_seconds=300)
	print(f"[warm] deploy-on-warm OK in {time.monotonic() - started:.0f}s ({DEPLOY_FQDN})")


def _verify_cold_fallback(server, snapshot, clones: list[str], warm_facts: dict) -> None:
	"""Tamper the captured host signature → the next clone must take the
	cold-boot fallback (vm-restore.py consumes the marker and fails the first
	launch; the relaunch boots with --config-file + --metadata): a FRESH
	boot_id, but still the clone's own identity and a serving site."""
	from atlas.atlas.deploy_site import wait_for_http

	_tamper(server.name, snapshot.memory_directory, "tamper")
	try:
		name = snapshot.clone_to_new_vm(
			title="warm-clone-cold-fallback",
			ssh_public_key=ephemeral_public_key() + "\n" + control_plane_public_key(),
			cpu_max_cores=CLONE_CPU_CAP,
		)
		frappe.db.commit()
		clones.append(name)
		wait_for_vm_running(name, timeout_seconds=300)
		# A cold boot is slower; give the probe a longer leash.
		facts = _guest_facts(server.name, name, wait_seconds=420)
		warm_boot_id = warm_facts[clones[0]]["FACT_BOOT_ID"]
		assert facts["FACT_BOOT_ID"] != warm_boot_id, (
			"fallback clone shares the golden boot_id — it restored despite the signature mismatch"
		)
		assert facts["FACT_ATLAS_VM_UUID"] == name, (
			f"cold fallback did not adopt identity via --metadata: {facts}"
		)
		vm = frappe.get_doc("Virtual Machine", name)
		wait_for_http(vm.ipv6_address, "site.local", timeout_seconds=420)
		print("[warm] cold-boot fallback verified (fresh boot_id, own identity, serving)")
	finally:
		_tamper(server.name, snapshot.memory_directory, "restore")


def _tamper(server_name: str, memory_directory: str, mode: str) -> None:
	from atlas.atlas.ssh import run_task

	task = run_task(
		server=server_name,
		script="warm-signature-tamper.sh",
		variables={"MEMORY_DIRECTORY": memory_directory, "MODE": mode},
		timeout_seconds=30,
	)
	assert task.status == "Success", f"signature {mode} failed: {(task.stderr or '')[:300]}"


def _guest_facts(server_name: str, virtual_machine_name: str, wait_seconds: int = 240) -> dict:
	"""Run the facts probe on the host and parse its FACT_* lines."""
	from atlas.atlas.ssh import run_task

	vm = frappe.get_doc("Virtual Machine", virtual_machine_name)
	task = run_task(
		server=server_name,
		script="warm-guest-facts.sh",
		variables={
			"VIRTUAL_MACHINE_IPV6": vm.ipv6_address,
			"SSH_PRIVATE_KEY": ephemeral_private_key(),
			"WAIT_SECONDS": str(wait_seconds),
		},
		virtual_machine=virtual_machine_name,
		timeout_seconds=wait_seconds + 60,
	)
	assert task.status == "Success", f"facts probe failed: {(task.stderr or '')[-500:]}"
	facts = {}
	for line in (task.stdout or "").splitlines():
		if line.startswith("FACT_"):
			key, _, value = line.partition("=")
			facts[key] = value.strip()
	print(f"[warm] {virtual_machine_name}: {facts}")
	return facts


def _terminate_quietly(virtual_machine_name: str) -> None:
	try:
		vm = frappe.get_doc("Virtual Machine", virtual_machine_name)
		if vm.status != "Terminated":
			vm.terminate()
		frappe.db.commit()
	except Exception as error:  # teardown must not mask the real failure
		print(f"[warm] teardown of {virtual_machine_name} failed: {error}")
