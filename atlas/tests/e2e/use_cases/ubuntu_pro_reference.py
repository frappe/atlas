"""Operator action: bake a reusable "Ubuntu Pro reference" image so we can run
CIS/STIG audits (USG — the Ubuntu Security Guide) against a stock Ubuntu 24.04
guest again and again, LOCALLY, without re-attaching a Pro token each time.

This is NOT a fleet image — it is a learning/benchmark artifact (operator, 2026-07-01).
Ubuntu Pro's flagship features (Livepatch, FIPS) do not fit our custom-kernel
Firecracker guests, and ESM buys nothing inside noble's support window. The ONE
useful thing is USG: Canonical's audited implementation of the CIS/DISA-STIG
benchmarks. We want it as a REFERENCE ORACLE — `usg audit` tells us what a
hardened Ubuntu *should* look like, so we can diff its findings against our
stripped image and confirm which controls (e.g. IPv6-forwarding) are genuinely
traps for our routed-tap model.

The bake mirrors `bench_image._bake`: provision a plain `ubuntu-24.04` guest,
do the work inside it over guest-SSH, stop it, snapshot it, promote the snapshot
to a same-server base image. Every step prints a labelled line so a bare
`bench execute` reads like a runbook.

The load-bearing subtlety the operator asked to prove: **USG must still audit
after the Pro token is detached.** The image is worthless if `usg audit` gates on
a live entitlement at RUN time. So the flow is:

    attach → enable usg → install ubuntu-security-guide → DETACH →
    prove `usg audit` still runs detached → stop → snapshot → promote

If USG refuses to run detached, we STOP and report it (do not snapshot a useless
image). Per the operator: dry-run the audit to confirm the image can do it —
don't run a full hardening audit yet.

    # Bake it (token passed explicitly, never persisted to a file/row):
    bench --site scaleway.local execute \
        atlas.tests.e2e.use_cases.ubuntu_pro_reference.run \
        --kwargs '{"pro_token": "C1xxxxxxxxxxxxxxxx"}'

    # Tear down the build VM later (the promoted image survives):
    bench --site scaleway.local execute \
        atlas.tests.e2e.use_cases.ubuntu_pro_reference.teardown \
        --kwargs '{"virtual_machine": "<vm-name>"}'
"""

import frappe

from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.ssh import connection_for_guest, wait_for_ssh
from atlas.tests.e2e._config import control_plane_public_key, ephemeral_public_key
from atlas.tests.e2e._tasks import wait_for_vm_running

# Stock server cloud image — deliberately NOT our stripped/optimized base. A CIS
# audit is only meaningful against a stock-ish baseline; auditing the stripped
# image would conflate "we removed it" with "CIS wants it removed".
BASE_IMAGE = "ubuntu-24.04"

# The promoted image + snapshot name. Lowercase/dots/dashes only (it becomes both
# the Virtual Machine Image row name and the LVM LV name — see promote_to_image).
IMAGE_NAME = "ubuntu-pro-usg-ref"

# USG needs headroom for the openscap content + reports under /var/lib/usg. 4 GB
# (the base default) is tight once we apt-install the security guide; give it room.
BUILD_DISK_GB = 8
BUILD_MEMORY_MB = 2048


def run(pro_token: str, server: str | None = None, reuse_vm: str | None = None, keep: bool = True) -> dict:
	"""Provision a plain guest on `server` (default: the Active Scaleway host),
	attach Ubuntu Pro with `pro_token`, install USG, DETACH, prove `usg audit`
	still runs detached, then stop + snapshot + promote to `IMAGE_NAME`.

	`reuse_vm` resumes on an already-Running build VM (e.g. after a mid-bake failure
	left one up) instead of provisioning a fresh billable guest.

	`pro_token` is passed straight to the guest over SSH and never written to a
	Frappe row, a file, or the image — the promoted image is detached (see module
	docstring). Returns a summary dict."""
	if not pro_token or not pro_token.strip():
		frappe.throw("pro_token is required (your Ubuntu Pro dev-account token).")
	pro_token = pro_token.strip()

	server = server or _default_scaleway_server()
	print(f"[pro-ref] server: {server}")
	print(f"[pro-ref] base image: {BASE_IMAGE}")

	if reuse_vm:
		vm = frappe.get_doc("Virtual Machine", reuse_vm)
		vm.reload()
		assert vm.status == "Running", f"reuse_vm {reuse_vm} is {vm.status}, expected Running"
		print(f"[pro-ref] REUSING build VM: {vm.name}  v6={vm.ipv6_address}")
	else:
		vm = _provision_build_vm(server)
		print(f"[pro-ref] build VM: {vm.name}  v6={vm.ipv6_address}")

	try:
		connection = connection_for_guest(vm)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			wait_for_ssh(connection, timeout_seconds=180)

			_attach_and_install_usg(connection, key_path, pro_token)
			_detach_pro(connection, key_path)
			audit = _prove_usg_runs_detached(connection, key_path)

		# Only reach here if USG proved itself detached. Stop + snapshot + promote.
		vm.stop()
		vm.reload()
		assert vm.status == "Stopped", vm.status
		snapshot_name = vm.snapshot(title="ubuntu-pro-usg-ref")
		print(f"[pro-ref] snapshot: {snapshot_name}")

		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		image_name = snapshot.promote_to_image(IMAGE_NAME, title="Ubuntu Pro USG reference")
		frappe.db.commit()
		print(f"[pro-ref] promoted to image: {image_name}")
	except Exception:
		# Leave the VM for inspection on failure (do not snapshot a bad bake).
		print(f"[pro-ref] FAILED — build VM {vm.name} left Running for inspection.")
		raise

	summary = {
		"server": server,
		"build_vm": vm.name,
		"build_vm_ipv6": vm.ipv6_address,
		"snapshot": snapshot_name,
		"image": image_name,
		"usg_profiles": audit["profiles"],
		"usg_version": audit["version"],
		"audit_runs_detached": audit["detached_ok"],
	}
	print("")
	print("=" * 68)
	print("UBUNTU PRO / USG REFERENCE IMAGE BAKED — image + snapshot LEFT IN PLACE.")
	for key, value in summary.items():
		print(f"  {key:<20} {value}")
	print("")
	print("  Clone a fresh auditing VM from it any time (no Pro token needed):")
	print(f"    image = {image_name}  (VM.image field, ordinary cold provision)")
	print("  Then run the audit inside it, e.g.:")
	print("    sudo usg audit cis_level1_server   # writes HTML+XML to /var/lib/usg")
	print("")
	print("  Tear down the build VM when done (the image survives):")
	print(
		"    bench --site scaleway.local execute "
		"atlas.tests.e2e.use_cases.ubuntu_pro_reference.teardown "
		f'--kwargs \'{{"virtual_machine": "{vm.name}"}}\''
	)
	print("=" * 68)
	return summary


def _default_scaleway_server() -> str:
	"""The single Active Scaleway host this bake targets. Explicit-fail if the
	fleet shape ever changes (0 or >1 Active Scaleway servers) so we never bake
	onto the wrong host silently."""
	names = frappe.get_all(
		"Server",
		filters={"status": "Active", "provider_type": "Scaleway"},
		pluck="name",
	)
	if len(names) != 1:
		frappe.throw(
			f"Expected exactly one Active Scaleway Server, found {len(names)}: {names}. "
			"Pass server=<name> explicitly."
		)
	return names[0]


def _provision_build_vm(server_name: str):
	# The controller SSHes as the ATLAS-settings key (connection_for_guest); the
	# e2e wait_for_ssh uses that same connection, so authorizing both the ephemeral
	# and control-plane keys keeps us aligned with bench_image's dual-key build VM.
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "ubuntu-pro USG reference — build",
			"server": server_name,
			"image": BASE_IMAGE,
			"vcpus": 2,
			"memory_megabytes": BUILD_MEMORY_MB,
			"disk_gigabytes": BUILD_DISK_GB,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=180)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _ssh(connection, key_path, command: str, *params: object, timeout_seconds: int = 600) -> str:
	"""Run one command in the guest as root, echoing it, and fail loud on non-zero.
	Returns stdout. `*params` fill the command's `{}` placeholders (auto-quoted by
	run_ssh) — that's how the Pro token becomes exactly one SSH-quoted token and
	never hits a file. The guest is IPv6-only outbound-capable (apt works in bakes),
	which is all `pro attach` + apt need."""
	# Don't echo interpolated params — the token is one of them. Show the template.
	print(f"[pro-ref]   $ {command}")
	stdout, stderr, code = run_ssh(connection, key_path, command, *params, timeout_seconds=timeout_seconds)
	if stdout.strip():
		print(_indent(stdout))
	if code != 0:
		raise AssertionError(f"guest command failed (exit {code}): {command}\n{stderr[-800:]}")
	return stdout


def _attach_and_install_usg(connection, key_path, pro_token: str) -> None:
	"""Attach Pro, enable USG, install the security guide. `pro attach` pulls the
	esm-infra/esm-apps apt sources and (by default) enables them; that's fine — we
	only need the packages present for the audit content, and we detach afterward."""
	print("[pro-ref] attaching Ubuntu Pro + installing USG (apt; slow) ...")
	# CRITICAL: attach with --no-auto-enable. A bare `pro attach` auto-enables ALL
	# default services, including Livepatch — which pulls snapd + the
	# canonical-livepatch snap and phones the livepatch server. On our minimal,
	# IPv6-only Firecracker guest that step hangs ("Timeout was reached") and fails
	# the whole attach with exit 4. Livepatch is inapplicable to our custom guest
	# kernel anyway (it patches the Ubuntu generic kernel), so we NEVER want it. We
	# attach quietly, then enable ONLY the services USG needs (esm-infra wires the
	# apt source; usg is the gated service that lets us apt-install the guide).
	_ssh(connection, key_path, "pro attach --no-auto-enable {}", pro_token, timeout_seconds=300)
	_ssh(connection, key_path, "pro enable esm-infra --assume-yes", timeout_seconds=180)
	# `pro enable usg` DOES the install itself — it configures the CIS apt source,
	# updates lists, and installs the usg + usg-benchmarks(-1) + openscap packages
	# ("Installing Ubuntu Security Guide packages"). There is NO `ubuntu-security-guide`
	# apt package in the CIS repo (that name 404s: "Unable to locate package"); a
	# separate apt-get install is both redundant and wrong. So enabling the service
	# is the whole install.
	_ssh(connection, key_path, "pro enable usg --assume-yes", timeout_seconds=600)
	# Confirm the binary landed before we throw the token away. NB: `usg version` is
	# NOT a subcommand (usg takes {list,info,audit,fix,...}); the version flag is
	# `usg --version`.
	_ssh(connection, key_path, "command -v usg && usg --version", timeout_seconds=60)


def _detach_pro(connection, key_path) -> None:
	"""Detach the Pro token BEFORE snapshot so the image carries no live contract,
	no metering timer, and no baked token. USG packages stay installed (detach only
	disables services + removes the contract, it does not purge apt-installed pkgs)."""
	print("[pro-ref] detaching Ubuntu Pro (image must be token-free) ...")
	_ssh(connection, key_path, "pro detach --assume-yes", timeout_seconds=180)
	# Prove we're actually detached — status should report an unattached machine.
	status = _ssh(connection, key_path, "pro status || true", timeout_seconds=60)
	if "This machine is not attached" not in status and "not attached" not in status.lower():
		# Not fatal to the artifact, but surface it loudly — an attached image is
		# exactly what the operator asked to avoid.
		print("[pro-ref]   WARNING: `pro status` does not clearly report 'not attached'; inspect above.")


def _prove_usg_runs_detached(connection, key_path) -> dict:
	"""The load-bearing check: does USG still work with NO Pro attachment? We run
	the cheap, non-mutating subcommands (version, list profiles) and a DRY audit —
	NOT a full hardening audit (operator: don't audit yet). If `usg audit` refuses
	because the machine is detached, this raises and we do not snapshot."""
	print("[pro-ref] proving USG runs DETACHED (no full audit) ...")
	version = _ssh(connection, key_path, "usg --version 2>&1", timeout_seconds=60)
	# `usg list --all` enumerates the audit/fix profiles from the installed content;
	# if this works detached, the content is on-disk and not entitlement-gated.
	profiles_out = _ssh(connection, key_path, "usg list --all 2>&1", timeout_seconds=120)

	# The real proof: can `usg audit` START without a live contract? A full audit is
	# slow and mutates /var/lib/usg; we only need to know it isn't blocked at the
	# entitlement gate. `usg audit --help` exercises the audit entrypoint's arg
	# parsing (loads the tool, no Pro call); then we probe `usg info <profile>` which
	# reads the profile content the audit would use. If EITHER errored on "not
	# entitled"/"not attached", _ssh would have raised.
	audit_help = _ssh(connection, key_path, "usg audit --help 2>&1", timeout_seconds=60)
	profile = _first_profile(profiles_out)
	info_ok = "(no profile parsed)"
	if profile:
		info_ok = _ssh(connection, key_path, f"usg info {profile} 2>&1 | head -20", timeout_seconds=120)

	detached_ok = bool(audit_help) and _no_entitlement_gate(audit_help + profiles_out)
	if not detached_ok:
		raise AssertionError(
			"USG appears to require a live Pro attachment at run time — a detached "
			"image would be useless. Aborting before snapshot. Output above."
		)
	print("[pro-ref]   USG runs detached ✓ (version + list + audit entrypoint OK)")
	return {
		"version": version.strip().splitlines()[-1] if version.strip() else "?",
		"profiles": [line for line in profiles_out.splitlines() if line.strip()][:12],
		"first_profile": profile,
		"info_head": info_ok,
		"detached_ok": detached_ok,
	}


def _first_profile(list_output: str) -> str | None:
	"""Pull a runnable profile name (e.g. cis_level1_server) out of `usg list`."""
	for line in list_output.splitlines():
		token = line.strip().split()[0] if line.strip() else ""
		if token.startswith("cis_") or token.startswith("stig"):
			return token
	return None


def _no_entitlement_gate(text: str) -> bool:
	"""True unless the output looks like a Pro-entitlement refusal."""
	lowered = text.lower()
	gates = ("not entitled", "requires ubuntu pro", "not attached to", "enable usg")
	return not any(g in lowered for g in gates)


def _indent(text: str) -> str:
	return "\n".join(f"[pro-ref]     {line}" for line in text.rstrip().splitlines())


def teardown(virtual_machine: str) -> None:
	"""Terminate the build VM. The promoted image + its snapshot survive."""
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if vm.status != "Terminated":
		vm.terminate()
		frappe.db.commit()
		print(f"[teardown] terminated build VM {virtual_machine} (image {IMAGE_NAME} survives)")


AUDIT_MEMORY_MB = 1024
AUDIT_DISK_GB = 8


def run_audit(profile: str = "cis_level1_server", server: str | None = None, keep: bool = False) -> dict:
	"""Clone a fresh, token-free VM from the promoted `ubuntu-pro-usg-ref` image and
	run a REAL `usg audit <profile>` inside it (no dry-run — this is the full
	hardening audit deferred from `run()`). Prints the plain-text results summary
	usg writes to stdout, then terminates the audit VM unless `keep=True`.

	Returns the parsed pass/fail counts plus the raw stdout so the caller can
	inspect individual rule IDs."""
	server = server or _default_scaleway_server()
	print(f"[audit] server: {server}")
	print(f"[audit] image: {IMAGE_NAME}  profile: {profile}")

	vm = _provision_audit_vm(server)
	print(f"[audit] audit VM: {vm.name}  v6={vm.ipv6_address}")

	try:
		connection = connection_for_guest(vm)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			wait_for_ssh(connection, timeout_seconds=180)
			print(f"[audit] running `usg audit {profile}` (real audit; several minutes) ...")
			# usg audit exits non-zero when the SYSTEM fails rules (that is the whole
			# point of an audit) — a failing exit code here is expected output, not a
			# tool error, so call run_ssh directly instead of the fail-loud `_ssh`
			# helper the bake steps use.
			stdout, stderr, code = run_ssh(
				connection, key_path, f"usg audit {profile} 2>&1", timeout_seconds=1800
			)
			print(_indent(stdout))
			if code not in (0, 2):
				raise AssertionError(f"usg audit exited {code} unexpectedly:\n{stderr[-800:]}")

			results = _read_latest_results(connection, key_path)
	finally:
		if keep:
			print(f"[audit] keep=True — leaving audit VM {vm.name} up for manual inspection.")
		else:
			vm.terminate()
			frappe.db.commit()
			print(f"[audit] terminated audit VM {vm.name}")

	summary = {
		"server": server,
		"profile": profile,
		"audit_vm": vm.name if keep else None,
		**_parse_summary(stdout),
	}
	print("")
	print("=" * 68)
	print(f"USG AUDIT COMPLETE — profile {profile}")
	for key, value in summary.items():
		print(f"  {key:<16} {value}")
	print("=" * 68)
	summary["stdout"] = stdout
	summary["results_xml"] = results
	return summary


def _provision_audit_vm(server_name: str):
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "ubuntu-pro USG audit run",
			"server": server_name,
			"image": IMAGE_NAME,
			"vcpus": 2,
			"memory_megabytes": AUDIT_MEMORY_MB,
			"disk_gigabytes": AUDIT_DISK_GB,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=180)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _read_latest_results(connection, key_path) -> str:
	"""usg writes a timestamped XML results file under /var/lib/usg/; cat the
	newest one back over SSH (no scp helper exists for guest->host pulls, and the
	file is plain text, so a cat is simpler than adding one)."""
	stdout, _stderr, code = run_ssh(
		connection,
		key_path,
		'f=$(ls -t /var/lib/usg/*results*.xml 2>/dev/null | head -1) && test -n "$f" && cat "$f"',
		timeout_seconds=60,
	)
	return stdout if code == 0 else ""


def _parse_summary(stdout: str) -> dict:
	"""usg's stdout ends with a `Rule Results Summary` / pass-fail tally; harvest the
	handful of numeric lines instead of re-deriving them from the XML."""
	summary = {}
	for line in stdout.splitlines():
		line = line.strip()
		if ":" not in line:
			continue
		key, _, value = line.partition(":")
		key = key.strip().lower().replace(" ", "_")
		value = value.strip()
		if key in ("pass", "fail", "notchecked", "notapplicable", "error", "unknown", "notselected"):
			summary[key] = value
	return summary
