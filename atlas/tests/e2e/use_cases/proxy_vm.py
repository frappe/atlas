"""Use case: build a proxy VM, route a site through it, prove inbound v4/v6.

This is the host-bound proof of the reverse proxy as a Virtual Machine
(spec/12-proxy.md "Pending"). Only a real droplet can show it — building nginx
inside a guest, the public-v6 south hop, the reserved-IP inbound v4, the live
map sync over guest SSH, and a zero-downtime rolling rebuild. The controller-side
logic (canonical JSON, the reconcile diff, the proxy-tree enumeration, the
reserved-IP NAT math) is unit-covered in milliseconds (atlas/atlas/test_proxy.py,
scripts/lib/atlas/test_reserved_ip_nat.py).

What it proves, end to end, with a REAL droplet + a REAL DigitalOcean reserved IP:

- **Build inside the guest** — Atlas SSHes into a fresh Ubuntu VM, uploads the
  committed proxy/ tree, and runs build.sh; nginx + Lua compiles and the unit
  comes up (atlas.atlas.proxy.build_proxy).
- **Guest-SSH map sync end-to-end** — Atlas reconciles the proxy's live map over
  SSH-to-the-guest (reconcile_proxy), then reads it back byte-for-byte.
- **inbound :80 to a site from the proxy's vantage** (the §2.1 release gate that
  had never been tested) — from inside the proxy guest, reach a site VM's
  public-v6 :80, the exact hop nginx's proxy_pass makes.
- **inbound :443 reachability** — attach a reserved IPv4 to the proxy, push the
  wildcard cert, and from OFF the droplet (the controller, over the public v4
  internet) hit https://<sub>.<region>.frappe.dev and get the site's response
  back through the proxy. This is the proxy's first real :443 listener.
- **rolling rebuild** — rebuild the proxy from its own snapshot, re-push the cert,
  re-sync the map, and confirm it serves again (the zero-downtime roll, §3.4).

It allocates a real reserved IP and provisions two VMs, so teardown is in a
`finally`: detach+release the (billable) reserved IP, then terminate both VMs.
"""

import subprocess
import time

import frappe

from atlas.atlas import proxy
from atlas.tests.e2e._shared import (
	assert_probe,
	control_plane_public_key,
	ensure_image_on_server,
	ephemeral_private_key,
	ephemeral_public_key,
	get_region,
	phase,
	wait_for_vm_running,
)

# A self-signed wildcard for the test region, generated once per run and pushed
# into the proxy guest. The controller-side :443 probe uses `curl -k`, so the
# cert only needs to be valid enough for nginx to serve TLS (CN match isn't
# asserted — reachability + routing is).
_TEST_SUBDOMAIN = "acme"


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path. Same as run_smoke today — the whole use case is host-bound, so
	there is no extra unit-coverable layer to add here (kept for symmetry with the
	other use cases' run/run_smoke split; the controller logic is unit-tested in
	atlas/atlas/test_proxy.py)."""
	run_smoke(reuse=reuse, keep=keep)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	with phase("proxy-vm (smoke)", reuse=reuse, keep=keep) as server:
		region = get_region()
		image = ensure_image_on_server(server.name).name
		proxy_vm = _provision_proxy_vm(server.name, image, region)
		site_vm = _provision_site_vm(server.name, image)
		reserved = None
		try:
			# 1. Build nginx+Lua inside the proxy guest (slow: compiles from source).
			proxy.build_proxy(proxy_vm.name)

			# 2. Stand up a tiny site server on the site VM's :80 (the upstream the
			#    proxy will route to), and map a subdomain at it.
			marker = f"marker-{site_vm.name[:8]}"
			_start_site_server(server.name, site_vm, marker)
			_make_subdomain(_TEST_SUBDOMAIN, site_vm.name, region)

			# 3. Sync the map into the proxy's live dict over guest SSH, read back.
			synced = proxy.reconcile_proxy(proxy_vm.name)
			assert synced, "first reconcile should have drifted (fresh proxy, empty dict)"
			_assert_live_map(proxy_vm.name, {_TEST_SUBDOMAIN: site_vm.ipv6_address})

			# 4. §2.1 release gate: the proxy can reach the site's :80 over public v6.
			assert_probe(
				server.name,
				"phase-proxy-site-from-vantage.sh",
				timeout_seconds=180,
				PROXY_IPV6=proxy_vm.ipv6_address,
				SITE_IPV6=site_vm.ipv6_address,
				SITE_MARKER=marker,
				SSH_PRIVATE_KEY=ephemeral_private_key(),
			)

			# 5. Attach a reserved v4, push the wildcard cert, and prove inbound :443
			#    from off the droplet routes through the proxy to the site.
			reserved = _allocate_and_attach(server.name, proxy_vm.name)
			reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")
			_push_test_cert(proxy_vm.name, region)
			_assert_inbound_https_routes_to_site(reserved_ipv4, region, marker)

			# 6. Rolling rebuild: snapshot, rebuild the proxy from it, re-push cert,
			#    re-sync, and confirm it serves again.
			_rolling_rebuild(proxy_vm.name, region, reserved_ipv4, marker)
		finally:
			_teardown(reserved, proxy_vm.name, site_vm.name)


def _provision_proxy_vm(server_name: str, image: str, region: str) -> "frappe.model.document.Document":
	# `region` is accepted for signature stability (callers pass the Atlas single
	# region positionally); the VM no longer carries a region field.
	# The proxy guest is reached two ways: host-side probes carry the EPHEMERAL key
	# (the vantage probe SSHes host->proxy), while the control plane
	# (proxy.build_proxy / reconcile_proxy / push_cert) reaches it via
	# connection_for_guest, which uses the ATLAS-settings key. In production the
	# proxy image bakes the Atlas key; here we provision the guest to trust BOTH
	# (authorized_keys is one key per line) so neither path is locked out.
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "proxy e2e — proxy",
			"server": server_name,
			"image": image,
			"is_proxy": 1,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _provision_site_vm(server_name: str, image: str) -> "frappe.model.document.Document":
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "proxy e2e — site",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _start_site_server(server_name: str, site_vm, marker: str) -> None:
	"""Launch the stand-in upstream on the site VM's [::]:80 (the e2e analog of the
	compose harness's vm-a/vm-b)."""
	assert_probe(
		server_name,
		"phase-proxy-start-site.sh",
		timeout_seconds=180,
		SITE_IPV6=site_vm.ipv6_address,
		SITE_MARKER=marker,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)


def _make_subdomain(subdomain: str, vm_name: str, region: str) -> None:
	# `region` is accepted for signature stability (callers pass it positionally),
	# but Subdomain no longer carries a region field — the proxy's live map keys on
	# the bare subdomain label and the region is the Atlas single region.
	frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": subdomain,
			"virtual_machine": vm_name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _assert_live_map(proxy_vm_name: str, expected: dict[str, str]) -> None:
	"""Read the proxy guest's live /map over guest SSH and assert it equals the
	expected map byte-for-byte (the same canonical compare reconcile uses)."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_guest

	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, stderr, code = run_ssh(
			connection, key_path, proxy._curl_command("GET", "/map"), timeout_seconds=60
		)
	assert code == 0, f"reading live /map failed: {stderr}"
	assert live == proxy.canonical_json(expected), (
		f"live map drifted from expected.\nlive:    {live!r}\nexpected:{proxy.canonical_json(expected)!r}"
	)
	print(f"[e2e] proxy live map matches desired ({len(expected)} entries) OK")


def _allocate_and_attach(server_name: str, vm_name: str) -> str:
	"""Reserve a real DO v4 and attach it to the proxy VM (vendor assign + host
	1:1-NAT Task). attach() denormalizes onto the VM."""
	from atlas.atlas.doctype.reserved_ip import reserved_ip as module

	reserved = module.allocate(server_name)
	frappe.db.commit()
	frappe.get_doc("Reserved IP", reserved).attach(vm_name)
	frappe.db.commit()
	return reserved


def _push_test_cert(proxy_vm_name: str, region: str) -> None:
	"""Generate a self-signed wildcard for the region and push it into the proxy
	guest (the cert-push path; build.sh leaves only a placeholder cert)."""
	fullchain, privkey = _self_signed_wildcard(region)
	proxy.push_cert(proxy_vm_name, fullchain=fullchain, privkey=privkey)


def _self_signed_wildcard(region: str) -> tuple[str, str]:
	"""A throwaway self-signed `*.<region>.frappe.dev` cert/key PEM pair, generated
	on the controller with openssl. The :443 probe uses curl -k, so this only needs
	to be a valid cert nginx will serve."""
	import os
	import subprocess as sp
	import tempfile

	with tempfile.TemporaryDirectory() as directory:
		key_path = os.path.join(directory, "privkey.pem")
		cert_path = os.path.join(directory, "fullchain.pem")
		sp.run(
			[
				"openssl",
				"req",
				"-x509",
				"-newkey",
				"rsa:2048",
				"-nodes",
				"-days",
				"2",
				"-keyout",
				key_path,
				"-out",
				cert_path,
				"-subj",
				f"/CN=*.{region}.frappe.dev",
				"-addext",
				f"subjectAltName=DNS:*.{region}.frappe.dev",
			],
			check=True,
			capture_output=True,
		)
		with open(cert_path) as handle:
			fullchain = handle.read()
		with open(key_path) as handle:
			privkey = handle.read()
	return fullchain, privkey


def _assert_inbound_https_routes_to_site(reserved_ipv4: str, region: str, marker: str) -> None:
	"""From the controller (off the droplet), HTTPS to the reserved v4 with the
	wildcard Host/SNI forced, and assert the site's marker comes back through the
	proxy. Proves: external v4 → DO edge → host DNAT → proxy :443 → router.lua →
	site :80 over v6 → response. Self-signed cert, so curl -k. Polls for the DO
	edge + DNAT + nginx to settle."""
	hostname = f"{_TEST_SUBDOMAIN}.{region}.frappe.dev"
	deadline = time.monotonic() + 180
	last = ""
	while time.monotonic() < deadline:
		try:
			result = subprocess.run(
				[
					"curl",
					"-4",
					"-k",
					"-sS",
					"--max-time",
					"15",
					"--resolve",
					f"{hostname}:443:{reserved_ipv4}",
					f"https://{hostname}/",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0 and marker in result.stdout:
				print(f"[e2e] inbound :443 {reserved_ipv4} -> proxy -> site ({marker}) OK")
				return
			last = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last = "curl timed out"
		time.sleep(5)
	raise AssertionError(
		f"inbound HTTPS to {reserved_ipv4} ({hostname}) never routed to the site within 180s "
		f"(last: {last!r}). The reserved-IP DNAT, the pushed cert, the live map, or the "
		f"proxy→site v6 hop is broken — or the controller has no v4 path to a DO reserved IP."
	)


def _rolling_rebuild(proxy_vm_name: str, region: str, reserved_ipv4: str, marker: str) -> None:
	"""Snapshot the proxy, rebuild it from that snapshot, re-push the cert, re-sync
	the map, and confirm it serves again — the zero-downtime roll done to one proxy
	(spec/12-proxy.md §3.4). Here we roll the single proxy and re-verify; in
	production DNS keeps the other 2-3 serving while one rolls."""
	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)

	vm.stop()
	frappe.db.commit()
	_wait_for_status(proxy_vm_name, "Stopped", timeout_seconds=120)

	snapshot_name = vm.snapshot(title="proxy-e2e-roll")
	frappe.db.commit()
	_wait_for_snapshot_available(snapshot_name, timeout_seconds=300)

	frappe.get_doc("Virtual Machine", proxy_vm_name).rebuild(source_type="snapshot", source=snapshot_name)
	frappe.db.commit()

	frappe.get_doc("Virtual Machine", proxy_vm_name).start()
	frappe.db.commit()
	_wait_for_status(proxy_vm_name, "Running", timeout_seconds=120)

	# A fresh boot from the snapshot already has the built stack; re-push the cert
	# and re-sync the map (the reconcile refills the dict on a fresh boot, §7.2),
	# then re-prove the front door.
	_push_test_cert(proxy_vm_name, region)
	proxy.reconcile_proxy(proxy_vm_name)
	_assert_inbound_https_routes_to_site(reserved_ipv4, region, marker)
	print("[e2e] rolling rebuild: proxy serves again after rebuild-from-snapshot OK")


def _wait_for_status(vm_name: str, status: str, timeout_seconds: int) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		current = frappe.db.get_value("Virtual Machine", vm_name, "status")
		if current == status:
			return
		time.sleep(2)
	raise AssertionError(f"VM {vm_name} did not reach {status} within {timeout_seconds}s")


def _wait_for_snapshot_available(snapshot_name: str, timeout_seconds: int) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "status")
		if status == "Available":
			return
		if status == "Failed":
			raise AssertionError(f"snapshot {snapshot_name} reached Failed")
		time.sleep(2)
	raise AssertionError(f"snapshot {snapshot_name} not Available within {timeout_seconds}s")


def _teardown(reserved: str | None, proxy_vm_name: str, site_vm_name: str) -> None:
	"""Always: detach + release the (billable) reserved IP, then terminate both
	VMs. Each step guarded so one failure doesn't strand the others."""
	if reserved and frappe.db.exists("Reserved IP", reserved):
		try:
			doc = frappe.get_doc("Reserved IP", reserved)
			if doc.virtual_machine:
				doc.detach()
			doc.release()
			frappe.db.commit()
		except Exception:
			import traceback

			print(f"[e2e] WARNING: reserved IP {reserved} teardown failed — release it by hand:")
			traceback.print_exc()
	for vm_name in (proxy_vm_name, site_vm_name):
		if frappe.db.exists("Virtual Machine", vm_name):
			vm = frappe.get_doc("Virtual Machine", vm_name)
			if vm.status not in ("Terminated",):
				try:
					vm.terminate()
					frappe.db.commit()
				except Exception:
					import traceback

					print(f"[e2e] WARNING: terminating {vm_name} failed:")
					traceback.print_exc()
