"""Operator action (NOT a test): provision a fresh server, stand up a proxy VM,
and wire it to serve a second VM over the full inbound-:443 path — then LEAVE IT
RUNNING.

This is the persist-the-infra sibling of `proxy_vm.run_smoke`. It reuses that
module's proven helpers (build_proxy / reconcile / cert push / reserved-IP
attach / the probes) but, unlike the smoke test, it:

  - provisions a BRAND-NEW server (reuse=False) — the operator asked for a new one,
  - stops after proving inbound HTTPS routes to the site (no rolling rebuild),
  - does NOT tear anything down — the server, both VMs, and the reserved IP stay
    up so the operator can inspect/use them (bills continue until terminated).

It is billable: one droplet + two Firecracker VMs + one DO reserved IPv4. Run it
on the operator's turn:

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.proxy_vm_provision.run

Teardown when done (reverse order, releases the billable reserved IP):

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.proxy_vm_provision.teardown \
        --kwargs '{"server": "<server-name>"}'
"""

import frappe

from atlas.atlas import proxy
from atlas.tests.e2e._config import get_region
from atlas.tests.e2e._droplets import ensure_bootstrapped_server
from atlas.tests.e2e._image import ensure_image_on_server

# Reuse the smoke test's helpers verbatim — same proven code paths, just driven
# without the teardown finally and without the rolling-rebuild step.
from atlas.tests.e2e.use_cases.proxy_vm import (
	_TEST_SUBDOMAIN,
	_allocate_and_attach,
	_assert_inbound_https_routes_to_site,
	_assert_live_map,
	_make_subdomain,
	_provision_proxy_vm,
	_provision_site_vm,
	_push_test_cert,
	_start_site_server,
)


def run() -> dict:
	"""Provision a new server + proxy VM + site VM, wire the proxy to serve the
	site, prove inbound HTTPS end to end, and KEEP everything running. Returns a
	summary dict (also printed)."""
	region = get_region()

	# A brand-new droplet (reuse=False), kept (keep=True) so it persists.
	print(f"[provision] standing up a NEW server in {region} ...")
	server, _client, _created = ensure_bootstrapped_server(reuse=False, keep=True)
	print(f"[provision] server ready: {server.name} (droplet {server.provider_resource_id})")
	return _wire(server, region)


def continue_on(server: str) -> dict:
	"""Resume the wiring on an EXISTING Active server (e.g. after the initial
	provision succeeded but a later step was interrupted). Same body as `run`
	minus the droplet provision — provisions the proxy + site VMs on the given
	server and wires them. Idempotent helpers, but it provisions fresh VMs each
	call, so only use it on a server that doesn't already have them."""
	region = get_region()
	server_doc = frappe.get_doc("Server", server)
	assert server_doc.status == "Active", f"server {server} is {server_doc.status}, not Active"
	print(f"[provision] continuing on existing server {server} (droplet {server_doc.provider_resource_id})")
	return _wire(server_doc, region)


def wire_existing(proxy_vm: str, site_vm: str) -> dict:
	"""Wire two ALREADY-PROVISIONED, Running VMs (a proxy and a site) on the same
	server — build the proxy stack, map the site, attach a reserved IP, prove
	inbound HTTPS. Use this to resume when the VMs exist but the wiring was
	interrupted (avoids provisioning duplicate VMs)."""
	proxy_doc = frappe.get_doc("Virtual Machine", proxy_vm)
	site_doc = frappe.get_doc("Virtual Machine", site_vm)
	assert proxy_doc.status == "Running", f"proxy VM {proxy_vm} is {proxy_doc.status}, not Running"
	assert site_doc.status == "Running", f"site VM {site_vm} is {site_doc.status}, not Running"
	assert proxy_doc.is_proxy, f"VM {proxy_vm} is not marked is_proxy"
	assert proxy_doc.server == site_doc.server, "proxy and site VMs must be on the same server"
	# VM no longer carries a region field; the region is the Atlas single region.
	region = get_region()
	server = frappe.get_doc("Server", proxy_doc.server)
	print(f"[provision] reusing existing VMs proxy={proxy_vm} site={site_vm} on server {server.name}")
	return _wire_vms(server, region, proxy_doc, site_doc)


def _wire(server, region: str) -> dict:
	image = ensure_image_on_server(server.name).name
	print(f"[provision] image on server: {image}")

	proxy_vm = _provision_proxy_vm(server.name, image, region)
	print(f"[provision] proxy VM: {proxy_vm.name}  v6={proxy_vm.ipv6_address}")
	site_vm = _provision_site_vm(server.name, image)
	print(f"[provision] site  VM: {site_vm.name}  v6={site_vm.ipv6_address}")
	return _wire_vms(server, region, proxy_vm, site_vm)


def _wire_vms(server, region: str, proxy_vm, site_vm) -> dict:
	# 1. Build nginx+Lua inside the proxy guest (slow: compiles from source).
	print("[wire] building the proxy stack inside the guest (compiles nginx+Lua) ...")
	proxy.build_proxy(proxy_vm.name)

	# 2. Stand up a stand-in site on the site VM's :80 and map a subdomain at it.
	marker = f"marker-{site_vm.name[:8]}"
	print(f"[wire] starting stand-in :80 server on the site VM (marker {marker}) ...")
	_start_site_server(server.name, site_vm, marker)
	_ensure_subdomain(_TEST_SUBDOMAIN, site_vm.name, region)
	hostname = f"{_TEST_SUBDOMAIN}.{region}.frappe.dev"
	print(f"[wire] Subdomain {hostname} -> {site_vm.name} ({site_vm.ipv6_address})")

	# 3. Reconcile the desired map into the proxy's live dict over guest SSH, read back.
	synced = proxy.reconcile_proxy(proxy_vm.name)
	assert synced, "first reconcile should have drifted (fresh proxy, empty dict)"
	_assert_live_map(proxy_vm.name, {_TEST_SUBDOMAIN: site_vm.ipv6_address})

	# 4. Prove the proxy can reach the site's :80 over public v6 (the §2.1 gate).
	from atlas.tests.e2e._config import ephemeral_private_key
	from atlas.tests.e2e._tasks import assert_probe

	print("[verify] proxy -> site over public v6 (:80) ...")
	assert_probe(
		server.name,
		"phase-proxy-site-from-vantage.sh",
		timeout_seconds=180,
		PROXY_IPV6=proxy_vm.ipv6_address,
		SITE_IPV6=site_vm.ipv6_address,
		SITE_MARKER=marker,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)

	# 5. Attach a reserved v4, push the wildcard cert, prove inbound :443 routes
	#    through the proxy to the site from OFF the droplet.
	return _finish_inbound(server, region, proxy_vm, site_vm, marker)


def finish_inbound(proxy_vm: str, site_vm: str) -> dict:
	"""Run only the inbound-v4 tail (reserved IP + cert + HTTPS proof) on a proxy
	that is already built, mapped, and serving over v6. Use this to complete after
	the core wiring succeeded but the reserved-IP step was interrupted — avoids
	rebuilding nginx and re-inserting the (now-existing) Subdomain."""
	proxy_doc = frappe.get_doc("Virtual Machine", proxy_vm)
	site_doc = frappe.get_doc("Virtual Machine", site_vm)
	# VM no longer carries a region field; the region is the Atlas single region.
	region = get_region()
	server = frappe.get_doc("Server", proxy_doc.server)
	marker = f"marker-{site_doc.name[:8]}"
	# The site server + subdomain + map are already in place from the core wiring;
	# make sure the stand-in :80 is still up and the subdomain still maps (idempotent).
	_start_site_server(server.name, site_doc, marker)
	_ensure_subdomain(_TEST_SUBDOMAIN, site_doc.name, region)
	proxy.reconcile_proxy(proxy_doc.name)
	return _finish_inbound(server, region, proxy_doc, site_doc, marker)


def _finish_inbound(server, region: str, proxy_vm, site_vm, marker: str) -> dict:
	hostname = f"{_TEST_SUBDOMAIN}.{region}.frappe.dev"
	print("[wire] allocating + attaching a reserved IPv4 to the proxy ...")
	reserved = _allocate_and_attach(server.name, proxy_vm.name)
	reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")
	print(f"[wire] reserved IPv4 {reserved_ipv4} attached; pushing wildcard cert ...")
	_push_test_cert(proxy_vm.name, region)
	print(f"[verify] inbound HTTPS https://{hostname} (-> {reserved_ipv4}) routes to the site ...")
	_assert_inbound_https_routes_to_site(reserved_ipv4, region, marker)

	summary = {
		"server": server.name,
		"droplet": server.provider_resource_id,
		"region": region,
		"proxy_vm": proxy_vm.name,
		"proxy_ipv6": proxy_vm.ipv6_address,
		"site_vm": site_vm.name,
		"site_ipv6": site_vm.ipv6_address,
		"reserved_ipv4": reserved_ipv4,
		"reserved_ip_row": reserved,
		"subdomain": hostname,
		"marker": marker,
	}
	print("")
	print("=" * 64)
	print("PROXY WIRED AND SERVING — infra LEFT RUNNING (bills until torn down).")
	for key, value in summary.items():
		print(f"  {key:<14} {value}")
	print("")
	print("  Reach it (self-signed cert, so -k):")
	print(f"    curl -4 -k --resolve {hostname}:443:{reserved_ipv4} https://{hostname}/")
	print("")
	print("  Tear down when done:")
	print(
		"    bench --site atlas.tests.local execute "
		"atlas.tests.e2e.use_cases.proxy_vm_provision.teardown "
		f'--kwargs \'{{"server": "{server.name}"}}\''
	)
	print("=" * 64)
	return summary


def _ensure_subdomain(subdomain: str, vm_name: str, region: str) -> None:
	"""Idempotent _make_subdomain: create the Subdomain row if absent, else leave
	the existing one (a re-run on already-wired infra must not duplicate-key)."""
	if frappe.db.exists("Subdomain", subdomain):
		return
	_make_subdomain(subdomain, vm_name, region)


def teardown(server: str) -> None:
	"""Tear down everything `run` left up for a given server: detach+release any
	reserved IP, terminate every VM on the server, then delete the droplet."""
	from atlas.tests.e2e._config import get_client
	from atlas.tests.e2e._droplets import cleanup_droplet

	# Reserved IPs first (release is billable to leave dangling).
	for reserved in frappe.get_all("Reserved IP", filters={"server": server}, pluck="name"):
		doc = frappe.get_doc("Reserved IP", reserved)
		try:
			if doc.virtual_machine:
				doc.detach()
			doc.release()
			frappe.db.commit()
			print(f"[teardown] reserved IP {reserved} detached + released")
		except Exception:
			import traceback

			print(f"[teardown] WARNING: reserved IP {reserved} cleanup failed — release by hand:")
			traceback.print_exc()

	for vm_name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
		vm = frappe.get_doc("Virtual Machine", vm_name)
		if vm.status != "Terminated":
			try:
				vm.terminate()
				frappe.db.commit()
				print(f"[teardown] terminated VM {vm_name}")
			except Exception:
				import traceback

				print(f"[teardown] WARNING: terminating {vm_name} failed:")
				traceback.print_exc()

	droplet_id = frappe.db.get_value("Server", server, "provider_resource_id")
	if droplet_id:
		cleanup_droplet(get_client(), int(droplet_id))
		print(f"[teardown] droplet {droplet_id} deleted")
