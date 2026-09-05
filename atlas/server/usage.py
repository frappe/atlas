from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import now_datetime

from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.virtual_machine_manager import VirtualMachineManager

if TYPE_CHECKING:
	from atlas.server.doctype.server.server import Server
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

USAGE_RETENTION = timedelta(hours=3)


def enqueue_server_syncs() -> None:
	"""Queue one state exchange for each ready server."""
	servers = frappe.get_all(
		"Server",
		filters={"status": "Running", "is_provisioning_completed": 1},
		pluck="name",
	)
	peers = get_wireguard_peers()
	privileged_addresses = get_privileged_vm_addresses()
	for server_name in servers:
		frappe.enqueue(
			sync_server,
			queue="default",
			timeout=30,
			server_name=server_name,
			wireguard_peers=peers,
			privileged_vm_addresses=privileged_addresses,
			job_id=f"atlas||server-sync||{server_name}",
			deduplicate=True,
		)


def sync_server(
	server_name: str,
	wireguard_peers: list[dict[str, Any]],
	privileged_vm_addresses: list[str],
) -> None:
	"""Exchange state with one host and store its capacity."""
	server = cast("Server", frappe.get_doc("Server", server_name))
	try:
		response = MetalClient(server).sync(wireguard_peers, get_desired_images(), privileged_vm_addresses)
		values = get_usage_values(response.get("capacity"))
	except MetalClientError, ValueError:
		frappe.log_error(frappe.get_traceback(), f"Could not sync Server {server.name}")
		return

	frappe.get_doc({"doctype": "Server Usage", "server": server.name, **values}).insert(
		ignore_permissions=True
	)


def get_desired_images() -> list[dict[str, Any]]:
	"""Return images that each host must retain locally."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={"enabled": 1, "status": "Available", "cache_image": 1},
		pluck="name",
	)
	return [
		cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", name)).get_desired_image()
		for name in names
	]


def get_privileged_vm_addresses() -> list[str]:
	"""Return the mesh addresses that Atlas WG Mesh permits across tenants."""
	virtual_machines = frappe.get_all(
		"Virtual Machine",
		filters={"is_privileged": 1, "is_draft": 0, "is_terminating": 0},
		fields=["name", "tenant_id"],
	)
	return [
		address
		for virtual_machine in virtual_machines
		if (address := VirtualMachineManager.get_wireguard_mesh_ipv6(virtual_machine))
	]


def get_wireguard_peers() -> list[dict[str, Any]]:
	servers = frappe.get_all(
		"Server",
		filters={"status": "Running", "is_provisioning_completed": 1},
		fields=["name", "wireguard_public_key", "public_ipv4_address", "port"],
	)
	peers = []
	for server in servers:
		if not server.wireguard_public_key or not server.public_ipv4_address:
			continue
		node_id = server.name.rsplit("-", 1)[-1]
		if not node_id.isdigit():
			continue
		peers.append(
			{
				"node": server.name,
				"node_id": int(node_id),
				"public_key": server.wireguard_public_key,
				"address": f"{server.public_ipv4_address}:{server.port}",
			}
		)
	return peers


def get_usage_values(usage: object) -> dict[str, int]:
	if not isinstance(usage, dict):
		raise ValueError("Metal capacity response must be an object")
	fields = (
		"total_cpu_count",
		"available_cpu_count",
		"virtual_machine_count",
		"total_memory_mib",
		"available_memory_mib",
		"total_storage_mib",
		"available_storage_mib",
	)
	if not all(
		isinstance(usage.get(field), int) and not isinstance(usage[field], bool) and usage[field] >= 0
		for field in fields
	):
		raise ValueError("Metal capacity response has invalid values")
	return {field: usage[field] for field in fields}


def delete_old_usage_samples() -> None:
	"""Delete capacity samples older than three hours."""
	cutoff = now_datetime() - USAGE_RETENTION
	frappe.db.delete("Server Usage", {"creation": ["<", cutoff]})
