"""Operator scratch harness to host-run the spec/18 push-only bench self-routing
e2e with the controller on the LAPTOP (atlas.tests.local), over public IPv6.

Why this exists: `self_serve_site.run_smoke` (which integrates the spec/18 proof)
is gated on TLS config (Route53/ACME) the tests site lacks. `bench_self_routing.run`
is the lighter harness (no TLS, no reserved IP) but assumes two running VMs already
exist. This module BUILDS those two VMs (a bench VM cloned from the golden + a proxy
VM), wires the laptop-controller reachability (host_name + per-VM /etc/hosts), then
the operator runs `atlas.tests.e2e.use_cases.bench_self_routing.run` against them. The
guest binary is the Phase-1 `bench-domain-provider` (the pilot plug-in, formerly
`atlas-route`) installed at `/usr/local/bin/bench-domain-provider`.

Provisions INLINE (no worker is running on the laptop; the after_insert enqueue is a
no-op). Idempotent-ish: `setup` reuses an Active server and the configured golden.

Steps:
    bench --site atlas.tests.local execute atlas.tests._routing_host_run.setup
        -> prints BENCH_VM=... PROXY_VM=...  (and injects /etc/hosts into both)
    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.bench_self_routing.run \
        --kwargs '{"bench_vm":"<bench>","proxy_vm":"<proxy>","terminate":false}'
    bench --site atlas.tests.local execute atlas.tests._routing_host_run.teardown \
        --kwargs '{"bench_vm":"<bench>","proxy_vm":"<proxy>"}'
"""

import os
import subprocess
import time

import frappe

from atlas.atlas import proxy
from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.ssh import connection_for_guest
from atlas.tests.e2e._config import (
	control_plane_public_key,
	ephemeral_public_key,
	get_region,
)
from atlas.tests.e2e._droplets import ensure_bootstrapped_server, ensure_e2e_provider
from atlas.tests.e2e._image import ensure_image_on_server


def _laptop_public_v6() -> str:
	"""The laptop's public IPv6 — the address each guest's /etc/hosts maps the
	controller hostname to. Resolved live so a changed prefix is picked up."""
	out = subprocess.run(
		["curl", "-s", "-6", "--max-time", "8", "https://api64.ipify.org"],
		check=True,
		capture_output=True,
		text=True,
	)
	addr = out.stdout.strip()
	assert ":" in addr, f"did not get a public IPv6 from ipify: {addr!r}"
	return addr


def _controller_host() -> str:
	"""The hostname half of the controller base URL guests POST to (from get_url,
	which honors the host_name we set to http://atlas.tests.local:8007)."""
	from urllib.parse import urlsplit

	url = frappe.utils.get_url()
	host = urlsplit(url).hostname
	assert host, f"could not derive controller host from get_url()={url!r}"
	return host


def _provision_inline(vm_name: str) -> None:
	"""Drive a freshly-inserted/cloned VM to Running INLINE (no worker on the laptop).

	`after_insert` enqueues `auto_provision`; on the dev bench that enqueue can run
	INLINE and race an explicit `provision()` (two provision-vm.py Tasks ~70ms apart →
	the loser dies on a duplicate snapshot LV, and provision()'s post-run `self.save()`
	then raises TimestampMismatchError because the failure path db_set the VM to Failed).

	So: re-fetch FRESH by name, and only provision if it's still Pending AND no
	provision Task already exists for it. If a provision already ran (status moved past
	Pending, or a Task exists), just wait it out — don't issue a second one."""
	already = frappe.db.count("Task", {"virtual_machine": vm_name, "script": "provision-vm.py"})
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status == "Pending" and already == 0:
		try:
			vm.provision()
		except frappe.TimestampMismatchError:
			# The inline enqueue raced us and won; its run is authoritative.
			pass
	# Settle: re-read the outcome (either provision flipped Running, or the failure path
	# flipped Failed). One short wait covers the inline-enqueue case.
	for _ in range(30):
		status = frappe.db.get_value("Virtual Machine", vm_name, "status")
		if status in ("Running", "Failed"):
			break
		time.sleep(1)
	status = frappe.db.get_value("Virtual Machine", vm_name, "status")
	assert status == "Running", f"VM {vm_name} did not reach Running (status={status})"


def _clear_known_host(ipv6: str) -> None:
	"""Drop any stale host key for this /128 from the Atlas known_hosts. A terminated
	VM's /128 is recycled onto a fresh clone with NEW host keys, so the transport's
	StrictHostKeyChecking=accept-new sees a CHANGED key and refuses. We made the clone
	and trust it, so removing the stale entry is safe (accept-new re-learns the new key)."""
	if not ipv6:
		return
	known_hosts = os.path.expanduser("~/.atlas/known_hosts")
	if not os.path.isfile(known_hosts):
		return
	subprocess.run(["ssh-keygen", "-R", ipv6, "-f", known_hosts], capture_output=True, text=True)


def _guest_raw(vm_name: str, command: str, timeout: int = 120):
	vm = frappe.get_doc("Virtual Machine", vm_name)
	_clear_known_host(vm.ipv6_address)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		return run_ssh(connection, key_path, command, timeout_seconds=timeout)


def _inject_hosts(vm_name: str, controller_host: str, laptop_v6: str) -> None:
	"""Map the controller hostname to the laptop's public v6 in the guest's
	/etc/hosts, so the in-guest atlas-route client (forced AF_INET6, Host=hostname)
	reaches the laptop while Frappe still resolves the site by Host header."""
	# Idempotent: drop any prior line for the host, then append the current mapping.
	cmd = (
		f"sed -i '/[[:space:]]{controller_host}$/d' /etc/hosts && "
		f"printf '%s %s\\n' '{laptop_v6}' '{controller_host}' >> /etc/hosts && "
		f"getent ahostsv6 {controller_host} | head -1"
	)
	stdout, stderr, code = _guest_raw(vm_name, cmd)
	assert code == 0, f"/etc/hosts inject failed on {vm_name}: {stderr[-300:]}"
	assert laptop_v6 in stdout, f"/etc/hosts did not resolve {controller_host}->{laptop_v6}: {stdout!r}"
	print(f"[host-run] {vm_name}: /etc/hosts {controller_host} -> {laptop_v6}  OK")


def _routing_client_source() -> str:
	"""The repo's current `bench-domain-provider` binary (bench/bench-domain-provider.py),
	read from the app source tree this site runs (apps/atlas -> the worktree)."""
	import atlas

	app_dir = os.path.dirname(os.path.dirname(os.path.abspath(atlas.__file__)))
	path = os.path.join(app_dir, "bench", "bench-domain-provider.py")
	with open(path) as handle:
		return handle.read()


def _install_routing_client(vm_name: str) -> None:
	"""Push the repo's CURRENT `bench-domain-provider` binary onto the guest at
	/usr/local/bin/bench-domain-provider.

	The golden snapshot was baked with an OLDER bench/ tree, so its baked binary may be
	the old `atlas-route` client (or a stale provider). Re-baking the golden is the durable
	fix; for THIS proof we install the exact repo binary in place so the run exercises the
	real Phase-1 contract (register/deregister/wildcard-domains/proxy-servers) on a real
	guest over v6."""
	source = _routing_client_source()
	# Ship via base64 to dodge any quoting/heredoc hazards over SSH.
	import base64

	b64 = base64.b64encode(source.encode()).decode()
	cmd = (
		f"printf '%s' '{b64}' | base64 -d | install -m 0755 /dev/stdin /usr/local/bin/bench-domain-provider "
		f"|| (printf '%s' '{b64}' | base64 -d > /tmp/bench-domain-provider && install -m 0755 /tmp/bench-domain-provider /usr/local/bin/bench-domain-provider); "
		f"head -1 /usr/local/bin/bench-domain-provider; wc -l < /usr/local/bin/bench-domain-provider"
	)
	stdout, stderr, code = _guest_raw(vm_name, cmd)
	assert code == 0, f"installing bench-domain-provider on {vm_name} failed: {stderr[-300:]}"
	print(f"[host-run] {vm_name}: installed repo bench-domain-provider ({stdout.strip()} lines)")


def _verify_bench_routing_client(vm_name: str) -> None:
	"""The bench VM must carry the Phase-1 `bench-domain-provider` binary (register/
	deregister/wildcard-domains/proxy-servers) + a routing env that points at THIS
	controller (cold injection). The golden's baked binary may be older, so we (re)install
	the repo binary first."""
	_install_routing_client(vm_name)
	# Confirm the new verbs are present (the usage line lists them).
	usage, _stderr, _code = _guest_raw(vm_name, "bench-domain-provider 2>&1 | head -3 || true")
	assert "register" in usage and "proxy-servers" in usage, (
		f"{vm_name} bench-domain-provider is not the Phase-1 binary (usage: {usage.strip()!r})"
	)
	stdout, stderr, _code = _guest_raw(
		vm_name,
		"test -x /usr/local/bin/bench-domain-provider && echo HAVE_CLIENT; cat /etc/atlas-routing.env 2>/dev/null || echo NO_ENV",
	)
	assert "HAVE_CLIENT" in stdout, (
		f"{vm_name} has NO /usr/local/bin/bench-domain-provider — the golden predates build.sh:5b; "
		f"re-bake the golden. (stderr: {stderr[-200:]})"
	)
	assert "ATLAS_BASE_URL=" in stdout, (
		f"{vm_name} /etc/atlas-routing.env missing ATLAS_BASE_URL (cold routing inject did not run): {stdout!r}"
	)
	print(f"[host-run] {vm_name}: bench-domain-provider + routing env present\n    {stdout.strip()}")


def setup() -> None:
	"""Build the two VMs (bench + proxy) and wire reachability. Prints the names."""
	ensure_e2e_provider()  # fixes Atlas Settings.ssh_private_key_path to the REAL key
	frappe.db.commit()

	server, _client, created = ensure_bootstrapped_server(reuse=True, keep=True)
	print(f"[host-run] server={server.name} (created_now={created}) ipv4={server.ipv4_address}")

	region = get_region()
	laptop_v6 = _laptop_public_v6()
	controller_host = _controller_host()
	print(f"[host-run] controller_host={controller_host}  laptop_public_v6={laptop_v6}")
	print(f"[host-run] guests will POST to {frappe.utils.get_url()} (resolved via /etc/hosts)")

	# --- proxy VM (reuse an existing built one if present) ----------------
	proxy_vm_name = _ensure_proxy(server.name, region)
	proxy_vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	print(f"[host-run] proxy_vm={proxy_vm_name} Running  v6={proxy_vm.ipv6_address}")

	# --- bench VM (reuse a running clone if present, else clone golden) ---
	bench_vm_name = _ensure_bench(server.name)
	bench_vm = frappe.get_doc("Virtual Machine", bench_vm_name)
	print(f"[host-run] bench_vm={bench_vm_name} Running  v6={bench_vm.ipv6_address}")

	# --- wire reachability + verify the client ---------------------------
	_inject_hosts(bench_vm_name, controller_host, laptop_v6)
	_inject_hosts(proxy_vm_name, controller_host, laptop_v6)
	_verify_bench_routing_client(bench_vm_name)

	# Sanity: the bench VM can actually reach the controller and the controller
	# resolves IT by source /128 (check_label should resolve THIS vm, returning ok
	# 'available' for a fresh label, NOT 'No bench VM resolves').
	stdout, _stderr, code = _guest_raw(
		bench_vm_name,
		f"curl -s -6 --max-time 12 -X POST "
		f"'{frappe.utils.get_url()}/api/method/atlas.atlas.bench_routing.check_label' "
		f"-d 'label=preflight-probe' -w '\\nHTTP_CODE=%{{http_code}}'",
	)
	print(f"[host-run] preflight check_label from bench guest:\n    {stdout.strip()}  (code={code})")

	print("")
	print("=" * 64)
	print(f"BENCH_VM={bench_vm_name}")
	print(f"PROXY_VM={proxy_vm_name}")
	print("=" * 64)


def _ensure_proxy(server_name: str, region: str) -> str:
	"""Return a Running, built proxy VM on the server — reuse an existing Running
	proxy this harness made (so a re-run doesn't re-provision/re-build), else build one."""
	existing = frappe.get_all(
		"Virtual Machine",
		filters={
			"server": server_name,
			"is_proxy": 1,
			"status": "Running",
			"title": ["like", "bench-self-routing e2e%"],
		},
		pluck="name",
	)
	if existing:
		name = existing[0]
		# Confirm it is SSH-reachable (a stale Running row would waste the whole run).
		_stdout, _stderr, code = _guest_raw(name, "true", timeout=20)
		if code == 0:
			print(f"[host-run] reusing existing proxy {name}")
			return name
		print(f"[host-run] existing proxy {name} unreachable; building a fresh one")

	base_image = ensure_image_on_server(server_name).name
	print(f"[host-run] base image on server: {base_image}")
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	proxy_vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "bench-self-routing e2e — proxy",
			"server": server_name,
			"image": base_image,
			"is_proxy": 1,
			"region": region,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	_provision_inline(proxy_vm.name)
	proxy.build_proxy(proxy_vm.name)
	print(f"[host-run] proxy built on {proxy_vm.name}")
	return proxy_vm.name


def _ensure_bench(server_name: str) -> str:
	"""Return a Running bench VM (a golden clone) — reuse a Running clone this harness
	made (so a re-run after a transient SSH/known_hosts blip doesn't re-clone), else
	clone the golden + provision it."""
	existing = frappe.get_all(
		"Virtual Machine",
		filters={
			"server": server_name,
			"is_proxy": 0,
			"status": "Running",
			"title": ["like", "bench-self-routing e2e — bench%"],
		},
		pluck="name",
	)
	if existing:
		name = existing[0]
		_stdout, _stderr, code = _guest_raw(name, "true", timeout=20)
		if code == 0:
			print(f"[host-run] reusing existing bench VM {name}")
			return name
		print(f"[host-run] existing bench VM {name} unreachable; cloning a fresh one")

	golden = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	assert golden, "Atlas Settings.default_bench_snapshot is unset"
	snap = frappe.get_doc("Virtual Machine Snapshot", golden)
	assert snap.status == "Available", f"golden {golden} is not Available (status={snap.status})"
	print(f"[host-run] cloning bench VM from golden {golden} (kind={snap.kind}) ...")
	bench_vm_name = snap.clone_to_new_vm(
		title="bench-self-routing e2e — bench",
		ssh_public_key=ephemeral_public_key() + "\n" + control_plane_public_key(),
	)
	frappe.db.commit()
	_provision_inline(bench_vm_name)
	return bench_vm_name


def teardown(bench_vm: str = "", proxy_vm: str = "") -> None:
	"""Terminate the two VMs built by setup (idempotent; safe from any status)."""
	for name in (bench_vm, proxy_vm):
		if name and frappe.db.exists("Virtual Machine", name):
			vm = frappe.get_doc("Virtual Machine", name)
			if vm.status != "Terminated":
				print(f"[host-run] terminating {name} (status={vm.status}) ...")
				vm.terminate()
				print(f"[host-run] terminated {name}")
			else:
				print(f"[host-run] {name} already Terminated")
	frappe.db.commit()
	# Clean any Subdomain rows the run might have stranded.
	for label in ("ws-e2e-test", "stray-e2e-test", "fail-e2e-test", "ws", "stray", "ws-fail"):
		if frappe.db.exists("Subdomain", label):
			frappe.delete_doc("Subdomain", label, force=1, ignore_permissions=True)
	frappe.db.commit()
	print("[host-run] teardown complete")
