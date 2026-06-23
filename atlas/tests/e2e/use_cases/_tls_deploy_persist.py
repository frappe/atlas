"""One-off operator driver (NOT a test): deploy the REAL LE-issued wildcard onto a
freshly-built proxy VM, validate inbound :443 end to end, and LEAVE IT RUNNING.

This is the persist-the-infra sibling of `tls_issuance.run_smoke`. It reuses that
module's helpers verbatim — issue (or reuse) the real cert, provision a proxy +
site VM, build nginx in the guest, push the LE cert via the producer's own
`push_to_proxies`, prove inbound HTTPS routes to the site under the issued
wildcard, and confirm the served leaf is byte-for-byte the issued cert. Unlike the
smoke test it does NOT tear down — the proxy, site VM, and reserved IP stay up so
the operator can poke at the live proxy (bills until torn down).

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases._tls_deploy_persist.run

Teardown when done (releases the billable reserved IP, terminates both VMs):

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases._tls_deploy_persist.teardown \
        --kwargs '{"proxy_vm": "<proxy-name>", "site_vm": "<site-name>"}'
"""

import frappe

from atlas.atlas import proxy
from atlas.tests.e2e._config import get_tls_config
from atlas.tests.e2e._droplets import ensure_bootstrapped_server
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e.use_cases.tls_issuance import (
	_TEST_SUBDOMAIN,
	_allocate_and_attach,
	_assert_inbound_https_routes_to_domain,
	_assert_issue_task_recorded,
	_assert_live_map,
	_assert_real_cert_on_disk,
	_assert_route53_reachable,
	_assert_served_cert_is_issued,
	_issue_certificate,
	_make_subdomain,
	_preflight_controller_deps,
	_probe_hostname,
	_provision_proxy_vm,
	_provision_site_vm,
	_seed_tls_doctypes,
	_start_site_server,
)


def run() -> dict:
	"""Provision a proxy + site VM on the existing Active server, issue/reuse the
	real LE wildcard, push it, validate :443, and KEEP everything up. Returns a
	summary dict (also printed)."""
	config = get_tls_config()
	_preflight_controller_deps()
	region = config["region"]
	domain = config["domain"]

	server, _client, _created = ensure_bootstrapped_server(reuse=True, keep=True)
	image = ensure_image_on_server(server.name).name
	print(f"[deploy] reusing server {server.name} (droplet {server.provider_resource_id}); image {image}")

	# Producer chain: seed the TLS rows + issue the REAL cert on the controller.
	_seed_tls_doctypes(config)
	_assert_route53_reachable()
	cert_name = _issue_certificate(domain)
	_assert_real_cert_on_disk(cert_name, domain)
	_assert_issue_task_recorded(domain)

	# Proxy + site VMs (left running on purpose).
	proxy_vm = _provision_proxy_vm(server.name, image, region)
	site_vm = _provision_site_vm(server.name, image)
	print(f"[deploy] proxy VM {proxy_vm.name} v6={proxy_vm.ipv6_address}")
	print(f"[deploy] site  VM {site_vm.name} v6={site_vm.ipv6_address}")
	return _wire_and_validate(server, region, domain, cert_name, proxy_vm, site_vm)


def continue_on(proxy_vm: str, site_vm: str) -> dict:
	"""Resume on TWO ALREADY-PROVISIONED, Running VMs (a proxy and a site) — reissue
	(or reuse) the cert, build/reconcile/push, validate :443. Use this after a run
	got past provisioning but the controller→guest hop failed (e.g. the controller
	lacked IPv6); fix the network, then resume reusing the live VMs instead of
	provisioning a fresh pair."""
	config = get_tls_config()
	_preflight_controller_deps()
	region = config["region"]
	domain = config["domain"]

	proxy_doc = frappe.get_doc("Virtual Machine", proxy_vm)
	site_doc = frappe.get_doc("Virtual Machine", site_vm)
	assert proxy_doc.status == "Running", f"proxy VM {proxy_vm} is {proxy_doc.status}, not Running"
	assert site_doc.status == "Running", f"site VM {site_vm} is {site_doc.status}, not Running"
	assert proxy_doc.is_proxy, f"VM {proxy_vm} is not marked is_proxy"
	server = frappe.get_doc("Server", proxy_doc.server)

	_seed_tls_doctypes(config)
	_assert_route53_reachable()
	cert_name = _issue_certificate(domain)
	_assert_real_cert_on_disk(cert_name, domain)
	_assert_issue_task_recorded(domain)
	print(f"[deploy] resuming on proxy={proxy_vm} site={site_vm} (server {server.name})")
	return _wire_and_validate(server, region, domain, cert_name, proxy_doc, site_doc, resume=True)


def _wire_and_validate(server, region, domain, cert_name, proxy_vm, site_vm, resume: bool = False) -> dict:
	"""Build the proxy stack, map the site, push the LE cert, and validate :443.
	Shared by `run` (fresh VMs) and `continue_on` (existing VMs)."""
	print("[deploy] building nginx+Lua inside the proxy guest (compiles from source) ...")
	proxy.build_proxy(proxy_vm.name)

	marker = f"marker-{site_vm.name[:8]}"
	_start_site_server(server.name, site_vm, marker)
	# A Subdomain's target VM is immutable once written, so a leftover row from a
	# prior run points at a now-dead VM. Drop it and recreate against THIS site VM
	# (idempotent: only recreate when absent or mis-targeted).
	existing_target = frappe.db.get_value("Subdomain", _TEST_SUBDOMAIN, "virtual_machine")
	if existing_target and existing_target != site_vm.name:
		frappe.delete_doc("Subdomain", _TEST_SUBDOMAIN, force=1, ignore_permissions=True)
		frappe.db.commit()
		existing_target = None
	if not existing_target:
		_make_subdomain(_TEST_SUBDOMAIN, site_vm.name, region)

	synced = proxy.reconcile_proxy(proxy_vm.name)
	if not resume:
		assert synced, "first reconcile should have drifted (fresh proxy, empty dict)"
	_assert_live_map(proxy_vm.name, {_TEST_SUBDOMAIN: site_vm.ipv6_address})

	reserved = frappe.db.get_value("Reserved IP", {"virtual_machine": proxy_vm.name}, "name")
	if not reserved:
		reserved = _allocate_and_attach(server.name, proxy_vm.name)
	reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")
	print(f"[deploy] reserved IPv4 {reserved_ipv4} attached; pushing the LE cert via push_to_proxies ...")

	pushed = frappe.get_doc("TLS Certificate", cert_name).push_to_proxies()
	assert proxy_vm.name in pushed, f"_push_to_proxies did not reach the proxy VM; pushed={pushed}"

	_assert_inbound_https_routes_to_domain(reserved_ipv4, domain, marker)
	_assert_served_cert_is_issued(reserved_ipv4, domain, cert_name)

	hostname = _probe_hostname(domain)
	summary = {
		"server": server.name,
		"droplet": server.provider_resource_id,
		"region": region,
		"domain": domain,
		"cert": cert_name,
		"proxy_vm": proxy_vm.name,
		"proxy_ipv6": proxy_vm.ipv6_address,
		"site_vm": site_vm.name,
		"site_ipv6": site_vm.ipv6_address,
		"reserved_ipv4": reserved_ipv4,
		"reserved_ip_row": reserved,
		"hostname": hostname,
		"marker": marker,
	}
	print("")
	print("=" * 64)
	print("LE WILDCARD DEPLOYED ON THE PROXY — infra LEFT RUNNING (bills until torn down).")
	for key, value in summary.items():
		print(f"  {key:<15} {value}")
	print("")
	print("  Reach it (LE STAGING cert is untrusted, so -k):")
	print(f"    curl -4 -k --resolve {hostname}:443:{reserved_ipv4} https://{hostname}/")
	print("")
	print("  Inspect the served cert (issuer = Let's Encrypt STAGING):")
	print(
		f"    openssl s_client -connect {reserved_ipv4}:443 -servername {hostname} </dev/null "
		"2>/dev/null | openssl x509 -noout -subject -issuer -dates"
	)
	print("")
	print("  Tear down when done:")
	print(
		"    bench --site atlas.tests.local execute "
		"atlas.tests.e2e.use_cases._tls_deploy_persist.teardown "
		f'--kwargs \'{{"proxy_vm": "{proxy_vm.name}", "site_vm": "{site_vm.name}"}}\''
	)
	print("=" * 64)
	return summary


def teardown(proxy_vm: str, site_vm: str) -> None:
	"""Detach+release the reserved IP attached to the proxy, terminate both VMs, and
	drop the TLS rows this deploy seeded. The droplet itself is left (it predates
	this run)."""
	from atlas.tests.e2e.use_cases.proxy_vm import _teardown

	reserved = frappe.db.get_value("Reserved IP", {"virtual_machine": proxy_vm}, "name")
	_teardown(reserved, proxy_vm, site_vm)

	domain = frappe.conf.get("atlas_tls_domain")
	for cert in frappe.get_all("TLS Certificate", filters={"root_domain": domain}, pluck="name"):
		frappe.delete_doc("TLS Certificate", cert, force=1, ignore_permissions=True)
	if frappe.db.exists("Root Domain", domain):
		frappe.delete_doc("Root Domain", domain, force=1, ignore_permissions=True)
	frappe.db.commit()
	print(f"[teardown] released reserved IP, terminated {proxy_vm[:8]} + {site_vm[:8]}, dropped TLS rows")
