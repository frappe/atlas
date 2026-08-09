"""Billable-droplet proof of the Phase-2 custom-domain SNI-passthrough contract
(spec/18 Component L, spec/12 § The stream front-door, spec/13 § Custom domains).

Proves, on real DO VMs over real IPv6, the custom-domain path the Phase-2 work added on
top of the Phase-1 `bench-domain-provider` contract:

  1. `bench-domain-provider register <custom-fqdn>` (a non-wildcard domain) reserves a
     `Custom Domain` row resolved by source /128, status=Active. After a proxy reconcile
     it is in BOTH the :80 ACME map and the :443 SNI map immediately — there is no
     readiness gate.
  2. `deregister` drops it from BOTH maps.

Reuses `_provider_billable_run`'s VM setup (caller + proxy) and `_routing_host_run`'s
reachability wiring, so it needs the SAME prerequisite: the controller reachable over the
laptop's PUBLIC IPv6 (see _provider_billable_run for the operator-authorization note).
Run on e2e.local:

    bench --site e2e.local execute atlas.tests._custom_domain_billable_run.run
    bench --site e2e.local execute atlas.tests._custom_domain_billable_run.teardown \\
        --kwargs '{"caller_vm":"<vm>","proxy_vm":"<proxy>"}'
"""

import json

import frappe

from atlas.atlas import proxy
from atlas.tests import _provider_billable_run as bdp
from atlas.tests import _routing_host_run as hr

# A custom (non-wildcard) external domain the caller "owns". It is deliberately NOT under
# the regional wildcard, so the guest binary takes the register_custom_domain path.
_CUSTOM_DOMAIN = "shop-e2e.example.com"


def _guest(vm_name: str, command: str, timeout: int = 120):
	return hr._guest_raw(vm_name, command, timeout)


def _read_live_sni_map(proxy_vm_name: str) -> dict:
	"""The proxy's live :443 SNI map (the stream `domains` dict) via the stream-admin
	GET-SNI line protocol over SSH-to-the-guest — the same transport proxy.py reconciles."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_guest

	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, stderr, code = run_ssh(
			connection, key_path, f"{proxy.STREAM_ADMIN_BIN} GET-SNI", timeout_seconds=60
		)
	assert code == 0, f"reading proxy SNI map failed: {stderr}"
	return json.loads(live) if live.strip() else {}


def _read_live_acme_map(proxy_vm_name: str) -> dict:
	"""The proxy's live :80 ACME map (the http `acme_domains` dict) via curl GET /acme."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_guest

	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, stderr, code = run_ssh(
			connection, key_path, proxy._curl_command("GET", "/acme"), timeout_seconds=60
		)
	assert code == 0, f"reading proxy ACME map failed: {stderr}"
	return json.loads(live) if live.strip() else {}


def run(caller_vm: str = "", proxy_vm: str = "", terminate: bool = False) -> None:
	"""Drive the Phase-2 custom-domain contract on real droplets over IPv6. Leaves the VMs
	running (terminate=False) so a re-run is cheap; clears its own Custom Domain rows."""
	hr.ensure_e2e_provider()
	frappe.db.commit()

	server, _client, _created = hr.ensure_bootstrapped_server(reuse=True, keep=True)
	region = hr.get_region()
	laptop_v6 = hr._laptop_public_v6()
	controller_host = hr._controller_host()
	print(f"[cd] server={server.name} controller_host={controller_host} laptop_v6={laptop_v6}")

	bdp._ensure_root_domain(region)
	frappe.db.commit()

	proxy_vm = proxy_vm or hr._ensure_proxy(server.name, region)
	proxy_doc = frappe.get_doc("Virtual Machine", proxy_vm)
	print(f"[cd] proxy_vm={proxy_vm} v6={proxy_doc.ipv6_address}")

	caller_vm = caller_vm or bdp._ensure_caller_vm(server.name, region)
	caller_doc = frappe.get_doc("Virtual Machine", caller_vm)
	site_v6 = caller_doc.ipv6_address
	print(f"[cd] caller_vm={caller_vm} v6={site_v6}")

	hr._inject_hosts(caller_vm, controller_host, laptop_v6)
	hr._install_routing_client(caller_vm)

	try:
		_check_register_active_in_both_maps(caller_vm, proxy_vm, site_v6)
		_check_deregister_drops_both_maps(caller_vm, proxy_vm)
	finally:
		_cleanup()
		if terminate:
			frappe.get_doc("Virtual Machine", caller_vm).terminate()
			frappe.get_doc("Virtual Machine", proxy_vm).terminate()
			frappe.db.commit()

	print("\n" + "=" * 64)
	print("custom-domain (Phase-2 SNI passthrough) billable proof: ALL CHECKS PASSED")
	print(f"  caller_vm={caller_vm}  proxy_vm={proxy_vm}  (still Running; pass terminate=true to drop)")
	print("=" * 64)


def _check_register_active_in_both_maps(caller_vm: str, proxy_vm: str, site_v6: str) -> None:
	print(f"\n[1] register custom domain {_CUSTOM_DOMAIN} → Active + BOTH proxy maps (no gate) ...")
	assert not frappe.db.exists("Custom Domain", _CUSTOM_DOMAIN), (
		"a stale Custom Domain exists before register"
	)
	_out, stderr, code = _guest(caller_vm, f"bench-domain-provider register {_CUSTOM_DOMAIN}")
	assert code == 0, f"register (custom) exit {code} (expected 0): {stderr[-400:]}"
	frappe.db.commit()  # refresh past the controller process's cross-process commit
	row = frappe.get_doc("Custom Domain", _CUSTOM_DOMAIN)
	assert row.virtual_machine == caller_vm and row.active, (
		f"register did not reserve {_CUSTOM_DOMAIN} for this VM (vm={row.virtual_machine}, active={row.active})"
	)
	assert row.status == "Active", f"a freshly registered custom domain must be Active, got {row.status}"
	# The trust root: caller resolution found THIS VM by source /128.
	audit = frappe.get_all(
		"Bench Routing Audit",
		filters={"endpoint": "register_custom_domain", "label": _CUSTOM_DOMAIN, "status": "ok"},
		fields=["vm", "source_ip"],
		order_by="creation desc",
		limit=1,
	)
	assert audit and audit[0]["vm"] == caller_vm, (
		f"register_custom_domain audit did not resolve this VM: {audit}"
	)
	assert audit[0]["source_ip"] == site_v6, (
		f"resolved /128 {audit[0]['source_ip']} != this VM's v6 {site_v6}"
	)

	proxy.reconcile_proxy(proxy_vm)
	acme = _read_live_acme_map(proxy_vm)
	sni = _read_live_sni_map(proxy_vm)
	# Both maps serve it immediately — no readiness gate: ACME carries the bare bracketed v6
	# (so the VM can run HTTP-01), SNI carries the [v6]:443 passthrough literal.
	assert acme.get(_CUSTOM_DOMAIN) == f"[{site_v6}]", (
		f"ACME map should serve {_CUSTOM_DOMAIN}→[{site_v6}]: {acme}"
	)
	assert sni.get(_CUSTOM_DOMAIN) == f"[{site_v6}]:443", (
		f"SNI map should serve {_CUSTOM_DOMAIN}→[{site_v6}]:443 immediately on register: {sni}"
	)
	print(
		f"[1] PASS — {_CUSTOM_DOMAIN} is Active, in the :80 ACME map ([{site_v6}]) AND the :443 SNI map ([{site_v6}]:443)"
	)


def _check_deregister_drops_both_maps(caller_vm: str, proxy_vm: str) -> None:
	print(f"\n[2] deregister {_CUSTOM_DOMAIN} drops it from BOTH proxy maps ...")
	_out, stderr, code = _guest(caller_vm, f"bench-domain-provider deregister {_CUSTOM_DOMAIN}")
	assert code == 0, f"deregister (custom) exit {code} (expected 0): {stderr[-400:]}"
	frappe.db.commit()
	assert not frappe.db.exists("Custom Domain", _CUSTOM_DOMAIN), (
		"deregister did not delete the Custom Domain"
	)
	proxy.reconcile_proxy(proxy_vm)
	sni = _read_live_sni_map(proxy_vm)
	acme = _read_live_acme_map(proxy_vm)
	assert _CUSTOM_DOMAIN not in sni, f"SNI map still serves {_CUSTOM_DOMAIN} after deregister: {sni}"
	assert _CUSTOM_DOMAIN not in acme, f"ACME map still serves {_CUSTOM_DOMAIN} after deregister: {acme}"
	print(f"[2] PASS — deregistered {_CUSTOM_DOMAIN} is gone from both the :443 SNI and :80 ACME maps")


def _cleanup() -> None:
	if frappe.db.exists("Custom Domain", _CUSTOM_DOMAIN):
		frappe.delete_doc("Custom Domain", _CUSTOM_DOMAIN, force=1, ignore_permissions=True)
	frappe.db.commit()


def teardown(caller_vm: str = "", proxy_vm: str = "") -> None:
	for name in (caller_vm, proxy_vm):
		if name and frappe.db.exists("Virtual Machine", name):
			vm = frappe.get_doc("Virtual Machine", name)
			if vm.status != "Terminated":
				vm.terminate()
				print(f"[cd] terminated {name}")
	frappe.db.commit()
	_cleanup()
	print("[cd] teardown complete")
