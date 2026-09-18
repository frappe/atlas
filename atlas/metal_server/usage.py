from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.metal_server.core.mesh_peers import get_mesh_peers
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.vm_state import store_reported_states

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

USAGE_RETENTION = timedelta(hours=3)


def enqueue_server_syncs() -> None:
	"""Queue one state exchange for each ready server."""
	servers = frappe.get_all(
		"Metal Server",
		filters={"status": "Running", "is_provisioning_completed": 1},
		pluck="name",
	)
	peers = get_wireguard_peers()
	privileged_addresses = get_privileged_vm_addresses()
	unicast_peers = get_desired_unicast_peers()
	for server_name in servers:
		enqueue_server_sync(server_name, peers, privileged_addresses, unicast_peers)


def enqueue_server_sync(
	server_name: str,
	wireguard_peers: list[dict[str, Any]] | None = None,
	privileged_vm_addresses: list[str] | None = None,
	unicast_peers: list[str] | None = None,
) -> None:
	"""Queue one state exchange. A caller that queues many syncs reads the shared
	sets once and supplies them."""
	if wireguard_peers is None:
		wireguard_peers = get_wireguard_peers()
	if privileged_vm_addresses is None:
		privileged_vm_addresses = get_privileged_vm_addresses()
	if unicast_peers is None:
		unicast_peers = get_desired_unicast_peers()

	frappe.enqueue(
		sync_server,
		queue="default",
		timeout=30,
		server_name=server_name,
		wireguard_peers=wireguard_peers,
		privileged_vm_addresses=privileged_vm_addresses,
		unicast_peers=unicast_peers,
		job_id=f"atlas||server-sync||{server_name}",
		deduplicate=True,
	)


def sync_server(
	server_name: str,
	wireguard_peers: list[dict[str, Any]],
	privileged_vm_addresses: list[str],
	unicast_peers: list[str] | None = None,
) -> None:
	"""Exchange state with one host, then store its capacity and VM states."""
	server = cast("MetalServer", frappe.get_doc("Metal Server", server_name))
	try:
		response = MetalClient(server).sync(
			wireguard_peers,
			get_desired_images(),
			privileged_vm_addresses,
			unicast_peers,
		)
		values = get_usage_values(response.get("capacity"))
		store_reported_states(server_name, response.get("virtual_machines"))
	except MetalClientError:
		frappe.log_error(
			frappe.get_traceback(),
			f"Metal synchronization failed for Server {server.name}",
		)
		return
	except ValueError:
		frappe.log_error(
			frappe.get_traceback(),
			f"Invalid synchronization response from Server {server.name}",
		)
		return

	frappe.get_doc({"doctype": "Metal Server Usage", "server": server.name, **values}).insert(
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
		if (address := get_virtual_machine_mesh_address(virtual_machine))
	]


def get_wireguard_peers() -> list[dict[str, Any]]:
	"""Return the complete managed WireGuard peer set for one host."""
	servers = frappe.get_all(
		"Metal Server",
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


def get_desired_unicast_peers() -> list[str] | None:
	"""Return the complete unicast peer set, or None in multicast mode.

	Metal decodes the sync request strictly, so the unicast field travels only in unicast mode.
	"""
	settings = frappe.get_single("Atlas Settings")
	if not settings.is_unicast_network_enabled:
		return None

	return get_mesh_peers()


def get_usage_values(usage: object) -> dict[str, int]:
	"""Return the capacity values to record from a Metal sync response."""
	if not isinstance(usage, dict):
		raise ValueError("Metal capacity response must be an object")
	fields = (
		"total_cpu_millicores",
		"available_cpu_millicores",
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
	frappe.db.delete("Metal Server Usage", {"creation": ["<", cutoff]})
