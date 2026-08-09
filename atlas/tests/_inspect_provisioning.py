import frappe


def run():
	"""Read-only: report DO settings, providers, and existing Servers.

	Used to decide whether/where to provision a server for UI-test capacity.
	"""
	from atlas.atlas.setup_catalog import default_name

	s = frappe.get_single("DigitalOcean Settings")
	token = s.get_password("api_token", raise_exception=False)
	size = default_name("Provider Size", "DigitalOcean")
	image = default_name("Provider Image", "DigitalOcean")
	print(f"SITE={frappe.local.site}")
	print(f"DO region={s.region!r} default_size={size!r} default_image={image!r} token_set={bool(token)}")
	servers = frappe.get_all(
		"Server",
		fields=["name", "title", "status", "provider_resource_id", "ipv4_address", "provider_type"],
	)
	if not servers:
		print("SERVERS none")
	for srv in servers:
		print(
			f"SERVER name={srv.name!r} title={srv.title!r} status={srv.status!r} "
			f"droplet={srv.provider_resource_id!r} ipv4={srv.ipv4_address!r} provider_type={srv.provider_type!r}"
		)


def list_droplets():
	"""Read-only: list LIVE droplets on the DO account (no deletes)."""
	from atlas.tests.e2e._config import get_client

	client = get_client()
	droplets = client.list_droplets_by_tag("atlas")
	print(f"LIVE droplets tagged 'atlas': {len(droplets)}")
	for d in droplets:
		net = d.get("networks", {}) or {}
		v4 = [n.get("ip_address") for n in net.get("v4", []) if n.get("type") == "public"]
		print(
			f"DROPLET id={d.get('id')} name={d.get('name')!r} status={d.get('status')!r} "
			f"region={(d.get('region') or {}).get('slug')!r} size={d.get('size_slug')!r} "
			f"ipv4={v4} created={d.get('created_at')!r}"
		)


def terminate_vm(name: str):
	"""Terminate one VM by name (frees host capacity, incl. partial Failed
	jails/LVs). terminate-vm.py is idempotent and callable from any non
	Terminated status. Keeps the row for history."""
	vm = frappe.get_doc("Virtual Machine", name)
	print(f"VM {name!r} status before = {vm.status!r}")
	if vm.status != "Terminated":
		vm.terminate()
		vm.reload()
	print(f"VM {name!r} status after = {vm.status!r}")


def vms_on_server(server: str):
	"""Read-only: list VMs placed on a Server and their status."""
	rows = frappe.get_all(
		"Virtual Machine",
		filters={"server": server},
		fields=["name", "title", "status", "ipv6_address"],
	)
	print(f"VMs on server {server!r}: {len(rows)}")
	for r in rows:
		print(f"  VM name={r.name!r} title={r.title!r} status={r.status!r} ipv6={r.ipv6_address!r}")
