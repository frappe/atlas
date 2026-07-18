"""Operator action (and host-bound proof): bake the golden bench image.

Provisions a plain Ubuntu VM, builds bench-cli + `bench init` inside it over
guest-SSH (`atlas.atlas.bench_image.build_bench`), stops it, and snapshots it.
That snapshot is the reusable "golden bench image" self-serve site VMs clone
from (`Virtual Machine Snapshot.clone_to_new_vm`) — the build-in-guest +
snapshot pattern the proxy uses, applied to bench (spec/08-images.md).

This is the ONE host fact the golden image exists to prove:
a VM baked this way actually has a working bench — `bench --version` responds
over guest-SSH after the build. Everything else about the image (the routing
identity, the site) is per-VM and lives in deploy-site.py (spec/14-self-serve.md).

It is billable: one droplet + one Firecracker VM (kept Stopped after the
snapshot, so it can be re-baked, or terminated once the snapshot exists). Run on
the operator's turn:

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.bench_image.run

`run_smoke` reuses the shared bootstrapped droplet (cheap); `run` provisions a
brand-new server. Both leave the snapshot row + its LV in place — that is the
artifact. Teardown when done:

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.bench_image.teardown \
        --kwargs '{"virtual_machine": "<vm-name>"}'
"""

import frappe

from atlas.atlas import bench_image
from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.image_recipes import get_recipe
from atlas.atlas.ssh import connection_for_guest
from atlas.tests.e2e._config import control_plane_public_key, ephemeral_public_key
from atlas.tests.e2e._droplets import phase
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e._tasks import wait_for_vm_running

# The bake clones Frappe + builds a uv venv + Node deps; 4 GB is too tight, so the
# build VM (and therefore the snapshot, and clones from it) gets a roomier disk and
# 2 GB RAM. These constants USED to live here, but the bench recipe is now the
# single source of truth for build-VM sizing (image_recipes._BENCH_DISK_GB /
# _BENCH_MEMORY_MB — bumped 12→20→28 as the ZFS vdev grew, see the comment there).
# Read them off the recipe so the e2e build VM can NEVER drift below what the bake
# actually needs again — the stale local `12` caused a yarn-step ENOSPC bake failure
# (root 100% full) that looked like a build bug but was just an undersized disk.
_BENCH_RECIPE = get_recipe("bench")
GOLDEN_DISK_GB = _BENCH_RECIPE.disk_gigabytes
GOLDEN_MEMORY_MB = _BENCH_RECIPE.memory_megabytes


def run(reuse: bool = False, keep: bool = True) -> dict:
	"""Provision a NEW server (reuse=False), bake the golden bench image on it,
	snapshot it, and leave the snapshot in place. Returns a summary dict."""
	with phase("bench-image", reuse=reuse, keep=keep) as server:
		return _bake(server.name)


def run_smoke(reuse: bool = True, keep: bool = True) -> dict:
	"""Dev-loop slice: reuse the shared bootstrapped droplet and bake there.

	Same bake + snapshot + `bench --version` proof as `run`, but on the shared
	server so we don't pay a fresh provision. The build itself (apt + clone + uv +
	node) is the slow part either way; reusing the droplet is the only saving."""
	with phase("bench-image (smoke)", reuse=reuse, keep=keep) as server:
		return _bake(server.name)


def _bake(server_name: str) -> dict:
	image = ensure_image_on_server(server_name)
	print(f"[bench-image] base image on server: {image.name}")

	vm = _provision_build_vm(server_name, image.name)
	print(f"[bench-image] build VM: {vm.name}  v6={vm.ipv6_address}")

	# 1. Bake bench-cli + `bench init` inside the guest (slow: apt + clone + uv).
	print("[bench-image] building bench inside the guest (apt + clone + uv + node) ...")
	bench_image.build_bench(vm.name)

	# 2. The host fact the golden image exists to prove: bench actually works in the guest.
	version = _assert_bench_works(vm)
	print(f"[bench-image] bench responds in the guest: {version}")

	# 3. Stop + snapshot. Snapshot requires a Stopped VM (clean unmount → no torn
	#    ext4). The snapshot is the golden image: site VMs clone from it.
	vm.stop()
	vm.reload()
	assert vm.status == "Stopped", vm.status
	snapshot_name = vm.snapshot(title="golden-bench")
	print(f"[bench-image] snapshot (golden image): {snapshot_name}")

	summary = {
		"server": server_name,
		"build_vm": vm.name,
		"build_vm_ipv6": vm.ipv6_address,
		"snapshot": snapshot_name,
		"bench_version": version,
	}
	print("")
	print("=" * 64)
	print("GOLDEN BENCH IMAGE BAKED — snapshot LEFT IN PLACE (the artifact).")
	for key, value in summary.items():
		print(f"  {key:<16} {value}")
	print("")
	print("  Site VMs clone from it via Virtual Machine Snapshot.clone_to_new_vm.")
	print("  Tear down the build VM when done (the snapshot survives):")
	print(
		"    bench --site atlas.tests.local execute "
		"atlas.tests.e2e.use_cases.bench_image.teardown "
		f'--kwargs \'{{"virtual_machine": "{vm.name}"}}\''
	)
	print("=" * 64)
	return summary


def _provision_build_vm(server_name: str, image: str) -> "frappe.model.document.Document":
	# build_bench reaches the guest via connection_for_guest (the ATLAS-settings
	# key), and the host-side `bench --version` probe SSHes with the EPHEMERAL key,
	# so the build VM must trust BOTH (authorized_keys is one key per line) — the
	# same dual-key shape the proxy VM uses.
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "golden bench — build",
			"server": server_name,
			"image": image,
			"vcpus": 2,
			"memory_megabytes": GOLDEN_MEMORY_MB,
			"disk_gigabytes": GOLDEN_DISK_GB,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _assert_bench_works(vm) -> str:
	"""SSH into the guest and run `bench -b atlas list-apps` AS the frappe user
	through its login shell (install.sh put bench-cli on PATH in the frappe user's
	~/.bashrc, and the bench is baked under that user). Proves the bake survived
	unsquash→pack→provision→boot and bench is actually invokable, not just present
	on disk. The controller SSHes in as root, so drop to frappe with `sudo -u`."""
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			# Drop to frappe and prepend bench-cli to PATH explicitly. A non-interactive
			# `bash -lc` does NOT source ~/.bashrc on Ubuntu (the default ~/.profile only
			# sources it for interactive shells, `case $- in *i*`), so the bench-cli PATH
			# install.sh writes there is absent and a bare `bench` exits 127 — the same
			# trap build.sh and the spec/18 self-routing step guard against by prepending
			# /home/frappe/pilot to PATH. Match them here, don't rely on .bashrc.
			"sudo -u frappe bash -lc 'export PATH=/home/frappe/pilot:$PATH; bench -b atlas list-apps'",
			timeout_seconds=120,
		)
	assert code == 0, f"bench did not run in the guest (exit {code}): {stderr[-500:]}"
	# list-apps prints the installed apps (frappe at minimum); assert frappe baked.
	assert "frappe" in stdout.lower(), f"frappe not found in baked bench: {stdout[-300:]}"
	return stdout.strip().splitlines()[-1] if stdout.strip() else "ok"


def teardown(virtual_machine: str) -> None:
	"""Terminate the build VM. The snapshot row + its LV survive (it is the
	golden image); delete the snapshot separately if rolling a new bake."""
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if vm.status != "Terminated":
		vm.terminate()
		frappe.db.commit()
		print(f"[teardown] terminated build VM {virtual_machine} (snapshot survives)")


# --------------------------------------------------------------------------- #
# Recipe-driven bake (the real Image Build product path) + admin-URL proof.
#
# The functions above bake by hand-rolling a build VM; the ones below drive the
# actual `Image Build` DocType lifecycle by recipe name — the path an operator's
# "Bake Image" dialog triggers — then promote and (for an admin recipe) prove the
# admin console renders end to end from the PROMOTED IMAGE (the customer path):
# create a VM with `image = <promoted>` → its build_mode is inherited from the
# image → deploy maps the FQDN to the admin app → curl `/api/status` over v6.
#
#     bench --site bootstrap.local execute \
#         atlas.tests.e2e.use_cases.bench_image.bake_promote_verify_admin \
#         --kwargs '{"recipe": "bench-v16-admin", "server": "<server>", \
#                    "fqdn": "v16admin.bootstrap.local"}'
# --------------------------------------------------------------------------- #

# The stock Ubuntu base the build VM boots from. Unlike _bake's DEFAULT_IMAGE sync,
# the recipe path takes an explicit base so the caller can point at whatever base
# image the bake server already carries (bootstrap.local syncs ubuntu-24.04).
BUILD_BASE_IMAGE = "ubuntu-24.04"


def bake_recipe(recipe: str, server: str, base_image: str = BUILD_BASE_IMAGE) -> dict:
	"""Bake one recipe through the real `Image Build` lifecycle, inline + synchronously.

	Inserts an `Image Build` row (its self-enqueue suppressed so we drive `run()`
	here, deterministically — the build VM's own boot still goes to the worker, which
	`run()` polls for) with `terminate_build_vm=False` so the build VM is left for
	inspection. Returns {"image_build", "build_vm", "snapshot"} once Available.

	auto_register is forced OFF: an admin recipe has no `registers_as`, and we never
	want a bake driver to silently repoint `default_bench_snapshot`."""
	from atlas.atlas.doctype.image_build import image_build as ib_module

	print(f"[bake_recipe] recipe={recipe} server={server} base={base_image}")
	build = _insert_image_build(recipe, server, base_image)
	# Run the lifecycle inline. run() provisions the build VM (its boot is enqueued to
	# the worker), waits for Running, uploads the tree + runs build.sh, stops, snapshots.
	# It commits at each transition. The worker may ALSO have picked up a stray enqueue;
	# run()'s Draft-guard makes the loser a no-op, but we suppressed the enqueue at
	# insert (below) so there is no stray. We do NOT patch enqueue here — run() needs
	# the VM's after_insert auto_provision to reach the worker.
	ib_module.run(build.name)
	build.reload()
	if build.status != "Available":
		raise AssertionError(f"bake of {recipe} ended {build.status}: {(build.error or '')[:500]}")
	print(
		f"[bake_recipe] {recipe} Available — build_vm={build.build_virtual_machine} snapshot={build.snapshot}"
	)
	return {
		"image_build": build.name,
		"build_vm": build.build_virtual_machine,
		"snapshot": build.snapshot,
	}


def _insert_image_build(recipe: str, server: str, base_image: str):
	"""Insert an Image Build row with its self-enqueue suppressed (we run() inline)."""
	from unittest.mock import patch

	from atlas.atlas.doctype.image_build import image_build as ib_module

	# Suppress ONLY the Image Build's own after_insert run-enqueue, so the worker
	# doesn't race our inline run(). Scoped to the insert; the VM boot enqueued later
	# (inside run) is unaffected.
	with patch.object(ib_module.frappe, "enqueue"):
		build = frappe.get_doc(
			{
				"doctype": "Image Build",
				"recipe": recipe,
				"server": server,
				"base_image": base_image,
				"auto_register": 0,
				"terminate_build_vm": 0,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return build


def promote_build(image_build: str) -> str:
	"""Promote a finished bake's snapshot into its series base image and return the
	image name. Thin wrapper over Image Build.promote (which defaults the image name to
	the recipe's `promote_image_name`, e.g. `bench-v16-admin`)."""
	build = frappe.get_doc("Image Build", image_build)
	image_name = build.promote()
	frappe.db.commit()
	print(f"[promote_build] {image_build} → image {image_name}")
	return image_name


def verify_admin_url(image_name: str, server: str, fqdn: str) -> dict:
	"""Create a VM from a promoted ADMIN image, deploy it, and prove the admin console
	renders at the FQDN — the customer path end to end.

	The VM is created with only `image = <promoted admin image>`; its build_mode is
	INHERITED from the image (set_build_mode_default), so deploy_site passes
	`--mode admin` without us restating it. We then probe the admin app's `/api/status`
	(200, unauthenticated — the admin console is Flask, no Frappe ping route) over the
	VM's public v6 /128, and fetch `/` to confirm the console HTML renders. Also
	asserts the deploy minted a one-click admin login URL (Pilot #117
	`bench generate-admin-session --full-path`) — the login-URL handoff (Phase B of
	llm/references/login-url-handoff-plan.md), proven here since there is no
	"Bench" doctype yet to persist it on (only Site has a login_url field today).

	Returns a dict with the VM name, the inherited build_mode, the minted
	login_url, and the two probe results. Leaves the VM Running for inspection."""
	from atlas.atlas.deploy_site import deploy_site, readiness_path_for_mode, wait_for_http

	ssh_public_key = ephemeral_public_key() + "\n" + control_plane_public_key()
	default_disk = frappe.db.get_value("Virtual Machine Image", image_name, "default_disk_gigabytes")
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"{fqdn} (admin from {image_name})",
			"server": server,
			"image": image_name,
			"vcpus": 2,
			"memory_megabytes": GOLDEN_MEMORY_MB,
			"disk_gigabytes": default_disk,
			"ssh_public_key": ssh_public_key,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	# The product assertion: a VM from an admin image is admin without anyone saying so.
	assert vm.build_mode == "admin", f"VM did not inherit admin mode from the image: {vm.build_mode!r}"
	print(f"[verify_admin_url] VM {vm.name} build_mode={vm.build_mode} v6={vm.ipv6_address}")

	wait_for_vm_running(vm.name, timeout_seconds=1500, poll_seconds=5)
	vm.reload()
	assert vm.status == "Running", vm.status

	# Deploy: maps the FQDN to the admin app ([admin].domain = <fqdn> + setup nginx)
	# and mints the admin login URL (bench generate-admin-session --full-path).
	print(f"[verify_admin_url] deploying {fqdn} in admin mode …")
	result = deploy_site(vm.name, fqdn)
	login_url = (result or {}).get("login_url", "")
	assert login_url, f"deploy_site returned no login_url for admin-mode {fqdn}: {result!r}"
	assert "sid=" in login_url, f"admin login_url has no sid: {login_url!r}"
	print(f"[verify_admin_url] admin login URL minted: {login_url}")

	# Probe the admin readiness path over the public v6 path (mode-aware = /api/status).
	path = readiness_path_for_mode("admin")
	print(f"[verify_admin_url] waiting for admin {path} on [{vm.ipv6_address}] (Host: {fqdn}) …")
	wait_for_http(vm.ipv6_address, fqdn, path=path, timeout_seconds=300)

	status = _curl_admin(vm.ipv6_address, fqdn, path)
	root = _curl_admin(vm.ipv6_address, fqdn, "/")
	summary = {
		"vm": vm.name,
		"vm_ipv6": vm.ipv6_address,
		"build_mode": vm.build_mode,
		"fqdn": fqdn,
		"login_url": login_url,
		"status_probe": status,
		"root_probe": root,
	}
	print("")
	print("=" * 64)
	print(f"ADMIN URL RENDERS — http://{fqdn}/  (over v6 [{vm.ipv6_address}])")
	for key, value in summary.items():
		print(f"  {key:<14} {value}")
	print("=" * 64)
	return summary


def _curl_admin(ipv6_address: str, host_header: str, path: str) -> dict:
	"""Fetch one admin URL over the VM's public v6 /128 from the controller host and
	return {http_code, content_type, snippet}. The admin app listens on :80 inside the
	guest behind nginx; the controller reaches the /128 over the public v6 internet
	(VMs are v6-inbound-only). Uses the system curl (already a controller dep)."""
	import subprocess

	url = f"http://[{ipv6_address}]:80{path}"
	result = subprocess.run(
		[
			"curl",
			"-s",
			"-m",
			"20",
			"-H",
			f"Host: {host_header}",
			"-w",
			"\n__HTTP__%{http_code}__CT__%{content_type}",
			url,
		],
		capture_output=True,
		text=True,
	)
	body, _, meta = result.stdout.rpartition("\n__HTTP__")
	code, _, content_type = meta.partition("__CT__")
	snippet = body.strip()[:200]
	probe = {"url": url, "http_code": code, "content_type": content_type, "snippet": snippet}
	print(f"[curl] {path} → {code} ({content_type}) {snippet[:100]!r}")
	return probe


def bake_promote_verify_admin(recipe: str, server: str, fqdn: str) -> dict:
	"""End-to-end admin proof for one recipe: bake → promote → create-VM-from-image →
	deploy → confirm the admin URL renders. The single entrypoint for the operator's
	turn (see module docstring). Leaves the build VM and the admin VM Running."""
	baked = bake_recipe(recipe, server)
	image_name = promote_build(baked["image_build"])
	verified = verify_admin_url(image_name, server, fqdn)
	return {"recipe": recipe, "image": image_name, **baked, "verify": verified}
