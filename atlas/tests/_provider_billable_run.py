"""Billable-droplet proof of the Phase-1 `bench-domain-provider` contract (spec/18
Component D, the move off `atlas-route`) — the part the rename actually changed.

Unlike `bench_self_routing.run`, this does NOT need a golden bench snapshot or
`bench new-site`: it proves the GUEST BINARY itself on a real DO VM over real IPv6 —
the IPv6 transport, caller resolution by source /128, the wildcard-suffix label peel,
the new verbs (`register`/`deregister`/`generate-dns-records`/`wildcard-domains`/
`proxy-servers`), the exit codes, and that the controller's proxy actually SERVES the
guest-reserved route and DROPS it on deregister. (The bench-cli-stack steps — new-site
serving the site, drop-site — are the golden-dependent slice covered by
`self_serve_site`/`bench_self_routing` once a golden is baked.)

Reuses `_routing_host_run`'s reachability wiring (laptop public v6 + per-VM /etc/hosts)
and proxy builder. Run on e2e.local (the billable site with a bootstrapped server):

    bench --site e2e.local execute atlas.tests._provider_billable_run.run
    bench --site e2e.local execute atlas.tests._provider_billable_run.teardown \\
        --kwargs '{"caller_vm":"<vm>","proxy_vm":"<proxy>"}'

PREREQUISITE — controller reachable over the laptop's PUBLIC IPv6 at :8007. `bench serve`
binds IPv4 only, while the guest connects over IPv6 only (caller resolution by source
/128), so the guest's POST is refused until an IPv6 listener fronts the dev server (e.g.
a `[::]:8007 -> 127.0.0.1:8007` forwarder, or running the controller bound to `::`). This
exposes the dev controller to the public v6 internet, so it needs deliberate operator
authorization — it is NOT done automatically by this harness.
"""

import json

import frappe

from atlas.atlas import proxy
from atlas.atlas.placement import active_root_domain
from atlas.tests import _routing_host_run as hr

_LABEL = "bdp-e2e"


def _guest(vm_name: str, command: str, timeout: int = 120):
	stdout, stderr, code = hr._guest_raw(vm_name, command, timeout)
	return stdout, stderr, code


def _read_live_map(proxy_vm_name: str) -> dict:
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_guest

	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, stderr, code = run_ssh(
			connection, key_path, proxy._curl_command("GET", "/map"), timeout_seconds=60
		)
	assert code == 0, f"reading proxy /map failed: {stderr}"
	return json.loads(live) if live.strip() else {}


def _ensure_root_domain(region: str) -> None:
	"""A single active Root Domain `<region>.frappe.dev` so `active_root_domain()` resolves
	the region wildcard the proxy terminates and the binary peels against. Idempotent; the
	billable site needs no real TLS for this proof (the proxy serves the live map by Host,
	not a cert)."""
	domain = f"{region}.frappe.dev"
	# Root Domain.validate requires the provider types to be set on Atlas Settings first
	# (mirrors test_bench_routing._ensure_root_domain); no real creds needed — the proxy
	# serves the live map by Host, not by a cert.
	if not frappe.db.get_single_value("Atlas Settings", "dns_provider_type"):
		frappe.db.set_single_value("Atlas Settings", "dns_provider_type", "Route53")
	if not frappe.db.get_single_value("Atlas Settings", "tls_provider_type"):
		frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt")
	if not frappe.db.exists("Root Domain", domain):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": domain,
				"region": region,
				"is_active": 1,
				"dns_provider_type": "Route53",
				"tls_provider_type": "Let's Encrypt",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Root Domain", domain, "is_active", 1)
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name != domain:
			frappe.db.set_value("Root Domain", name, "is_active", 0)
	print(f"[bdp] active Root Domain = {domain} (region {region})")


_CALLER_TITLE = "bdp e2e — caller"


def _ensure_caller_vm(server_name: str, region: str) -> str:
	"""A Running, non-proxy VM to act as the calling guest. We only need an OS +
	python3 + SSH (NOT the bench stack), so a fresh base-image VM is enough — no golden
	required. Reuse a Running one this harness made (if SSH-reachable with our key), else
	provision a fresh base-image VM with the ephemeral + control-plane keys authorized."""
	from atlas.tests.e2e._config import control_plane_public_key, ephemeral_public_key
	from atlas.tests.e2e._image import ensure_image_on_server

	existing = frappe.get_all(
		"Virtual Machine",
		filters={"server": server_name, "is_proxy": 0, "status": "Running", "title": _CALLER_TITLE},
		fields=["name", "ipv6_address"],
	)
	for row in existing:
		if not row["ipv6_address"]:
			continue
		_stdout, _stderr, code = hr._guest_raw(row["name"], "python3 --version", timeout=20)
		if code == 0:
			print(f"[bdp] reusing caller VM {row['name']} (v6={row['ipv6_address']})")
			return row["name"]

	base_image = ensure_image_on_server(server_name).name
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	caller = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": _CALLER_TITLE,
			"server": server_name,
			"image": base_image,
			"is_proxy": 0,
			"region": region,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	hr._provision_inline(caller.name)
	print(f"[bdp] provisioned fresh caller VM {caller.name}")
	return caller.name


def run(caller_vm: str = "", proxy_vm: str = "", terminate: bool = False) -> None:
	"""Drive the Phase-1 contract on real droplets over IPv6. Leaves the VMs running
	(terminate=False) so a re-run is cheap; clears its own Subdomain rows."""
	hr.ensure_e2e_provider()
	frappe.db.commit()

	server, _client, _created = hr.ensure_bootstrapped_server(reuse=True, keep=True)
	region = hr.get_region()
	laptop_v6 = hr._laptop_public_v6()
	controller_host = hr._controller_host()
	print(f"[bdp] server={server.name} controller_host={controller_host} laptop_v6={laptop_v6}")
	print(f"[bdp] guests POST to {frappe.utils.get_url()} (resolved via /etc/hosts)")

	_ensure_root_domain(region)
	frappe.db.commit()

	proxy_vm = proxy_vm or hr._ensure_proxy(server.name, region)
	proxy_doc = frappe.get_doc("Virtual Machine", proxy_vm)
	print(f"[bdp] proxy_vm={proxy_vm} v6={proxy_doc.ipv6_address}")

	caller_vm = caller_vm or _ensure_caller_vm(server.name, region)
	caller_doc = frappe.get_doc("Virtual Machine", caller_vm)
	site_v6 = caller_doc.ipv6_address
	print(f"[bdp] caller_vm={caller_vm} v6={site_v6}")

	hr._inject_hosts(caller_vm, controller_host, laptop_v6)
	hr._install_routing_client(caller_vm)

	domain = active_root_domain().domain
	fqdn = f"{_LABEL}.{domain}"

	try:
		_check_host_queries(caller_vm, proxy_vm, domain)
		_check_register_serves(caller_vm, proxy_vm, domain, fqdn, site_v6)
		_check_deregister_drops(caller_vm, proxy_vm, fqdn)
		_check_generate_dns_records(caller_vm, fqdn)
		_check_register_fail_closed(caller_vm)
	finally:
		_cleanup(_LABEL)
		if terminate:
			frappe.get_doc("Virtual Machine", caller_vm).terminate()
			frappe.get_doc("Virtual Machine", proxy_vm).terminate()
			frappe.db.commit()

	print("\n" + "=" * 64)
	print("bench-domain-provider Phase-1 billable proof: ALL CHECKS PASSED")
	print(f"  caller_vm={caller_vm}  proxy_vm={proxy_vm}  (still Running; pass terminate=true to drop)")
	print("=" * 64)


def _check_host_queries(caller_vm: str, proxy_vm: str, domain: str) -> None:
	print("\n[1] host-level queries (wildcard-domains + proxy-servers) ...")
	out, stderr, code = _guest(caller_vm, "bench-domain-provider wildcard-domains")
	assert code == 0, f"wildcard-domains exit {code}: {stderr[-300:]}"
	wildcards = json.loads(out.strip())
	assert wildcards == [f"*.{domain}"], f"wildcard-domains={wildcards!r} expected ['*.{domain}']"

	out, stderr, code = _guest(caller_vm, "bench-domain-provider proxy-servers")
	assert code == 0, f"proxy-servers exit {code}: {stderr[-300:]}"
	ips = json.loads(out.strip())
	proxy_v6 = frappe.db.get_value("Virtual Machine", proxy_vm, "ipv6_address")
	assert proxy_v6 in ips, f"proxy-servers {ips!r} missing this proxy's v6 {proxy_v6!r}"
	print(f"[1] PASS — wildcard-domains={wildcards} proxy-servers⊇{proxy_v6}")


def _check_register_serves(caller_vm: str, proxy_vm: str, domain: str, fqdn: str, site_v6: str) -> None:
	print(f"\n[2] register {fqdn} reserves (peeled to '{_LABEL}') + the proxy serves it ...")
	assert not frappe.db.exists("Subdomain", _LABEL), f"a stale Subdomain '{_LABEL}' exists before register"
	_out, stderr, code = _guest(caller_vm, f"bench-domain-provider register {fqdn}")
	assert code == 0, f"register exit {code} (expected 0): {stderr[-400:]}"
	# The guest's POST committed the Subdomain in the SEPARATE controller (bench serve)
	# process. This harness process holds its own transaction whose REPEATABLE-READ snapshot
	# was fixed by earlier reads, so it cannot see that cross-process commit until we end the
	# transaction. Commit to start a fresh snapshot before reading the guest-written row back.
	frappe.db.commit()
	row = frappe.get_doc("Subdomain", _LABEL)
	assert row.virtual_machine == caller_vm and row.active, (
		f"register did not reserve '{_LABEL}' for this VM (vm={row.virtual_machine}, active={row.active})"
	)
	# The trust root: caller resolution found THIS VM by its v6 source /128.
	audit = frappe.get_all(
		"Bench Routing Audit",
		filters={"endpoint": "register", "label": _LABEL, "status": "ok"},
		fields=["vm", "source_ip"],
		order_by="creation desc",
		limit=1,
	)
	assert audit and audit[0]["vm"] == caller_vm, f"register audit did not resolve this VM: {audit}"
	assert audit[0]["source_ip"] == site_v6, (
		f"register resolved source /128 {audit[0]['source_ip']} != this VM's v6 {site_v6}"
	)
	proxy.reconcile_proxy(proxy_vm)
	live = _read_live_map(proxy_vm)
	assert live.get(_LABEL) == site_v6, f"proxy live map does not serve {_LABEL} → {site_v6}: {live}"
	print(f"[2] PASS — guest-reserved {fqdn} resolved this VM by v6 source and is served by the proxy")


def _check_deregister_drops(caller_vm: str, proxy_vm: str, fqdn: str) -> None:
	print(f"\n[3] deregister {fqdn} drops the route from the proxy live map ...")
	_out, stderr, code = _guest(caller_vm, f"bench-domain-provider deregister {fqdn}")
	assert code == 0, f"deregister exit {code} (expected 0): {stderr[-400:]}"
	# Refresh the snapshot to see the controller process's cross-process delete (see [2]).
	frappe.db.commit()
	assert not frappe.db.exists("Subdomain", _LABEL), "deregister did not delete the route"
	proxy.reconcile_proxy(proxy_vm)
	live = _read_live_map(proxy_vm)
	assert _LABEL not in live, f"proxy live map still serves {_LABEL} after deregister: {live}"
	print(f"[3] PASS — deregistered {fqdn} is gone from the proxy's live map")


def _check_generate_dns_records(caller_vm: str, fqdn: str) -> None:
	print(f"\n[4] generate-dns-records {fqdn} prints {{}} (wildcard subdomain needs none) ...")
	out, stderr, code = _guest(caller_vm, f"bench-domain-provider generate-dns-records {fqdn} {fqdn}")
	assert code == 0, f"generate-dns-records exit {code}: {stderr[-300:]}"
	assert json.loads(out.strip()) == {}, f"expected {{}}, got {out.strip()!r}"
	print("[4] PASS — generate-dns-records returned {} for a wildcard subdomain")


def _check_register_fail_closed(caller_vm: str) -> None:
	print("\n[5] register fails CLOSED when the controller is unreachable (exit 1) ...")
	# Point the routing env at a closed v6 port so the POST has a v6 route but no listener.
	save = "cp /etc/atlas-routing.env /tmp/atlas-routing.env.bak"
	break_env = "printf 'ATLAS_BASE_URL=http://[::1]:1\\n' > /etc/atlas-routing.env"
	restore = "mv /tmp/atlas-routing.env.bak /etc/atlas-routing.env"
	_guest(caller_vm, save)
	try:
		_guest(caller_vm, break_env)
		_guest(caller_vm, "bench-domain-provider register x.example.invalid; echo EXIT=$?")
		# The binary peels against the (now-unreachable) controller for the suffix first,
		# so the wildcard_domains POST fails → transport → exit 1 (fail-closed).
		out2, _stderr2, _code2 = _guest(caller_vm, "bench-domain-provider register foo.bar; echo EXIT=$?")
		assert "EXIT=1" in out2, (
			f"register did not fail-closed (exit 1) on an unreachable controller: {out2!r}"
		)
	finally:
		_guest(caller_vm, restore)
	print("[5] PASS — register exited 1 (fail-closed) on an unreachable controller")


def _cleanup(*labels: str) -> None:
	for label in labels:
		if frappe.db.exists("Subdomain", label):
			frappe.delete_doc("Subdomain", label, force=1, ignore_permissions=True)
	frappe.db.commit()


def teardown(caller_vm: str = "", proxy_vm: str = "") -> None:
	for name in (caller_vm, proxy_vm):
		if name and frappe.db.exists("Virtual Machine", name):
			vm = frappe.get_doc("Virtual Machine", name)
			if vm.status != "Terminated":
				vm.terminate()
				print(f"[bdp] terminated {name}")
	frappe.db.commit()
	_cleanup(_LABEL)
	print("[bdp] teardown complete")
