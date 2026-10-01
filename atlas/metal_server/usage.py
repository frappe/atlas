from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.models import Route
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
	unicast = is_unicast_network_enabled()
	for server_name in servers:
		enqueue_server_sync(server_name, peers, privileged_addresses, unicast)


def enqueue_server_sync(
	server_name: str,
	wireguard_peers: list[dict[str, Any]] | None = None,
	privileged_vm_addresses: list[str] | None = None,
	unicast: bool | None = None,
) -> None:
	"""Queue one state exchange. A caller that queues many syncs reads the shared
	sets once and supplies them."""
	if wireguard_peers is None:
		wireguard_peers = get_wireguard_peers()
	if privileged_vm_addresses is None:
		privileged_vm_addresses = get_privileged_vm_addresses()
	if unicast is None:
		unicast = is_unicast_network_enabled()

	frappe.enqueue(
		sync_server,
		queue="default",
		timeout=30,
		server_name=server_name,
		wireguard_peers=wireguard_peers,
		privileged_vm_addresses=privileged_vm_addresses,
		unicast=unicast,
		job_id=f"atlas||server-sync||{server_name}",
		deduplicate=True,
	)


def sync_server(
	server_name: str,
	wireguard_peers: list[dict[str, Any]],
	privileged_vm_addresses: list[str],
	unicast: bool,
) -> None:
	"""Exchange state with one host, then store its capacity, VM states, and private network MAC."""
	server = cast("MetalServer", frappe.get_doc("Metal Server", server_name))
	try:
		response = MetalClient(server).sync(
			wireguard_peers,
			get_desired_images(),
			privileged_vm_addresses,
			unicast,
		)
		values = get_usage_values(response.get("capacity"))
		store_reported_states(server_name, response.get("virtual_machines"))
		sync_vm_gateway_routes(server_name, response.get("virtual_machines"))
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

	mac_address = response.get("private_network_mac_address")
	if mac_address and mac_address != server.private_network_mac_address:
		frappe.db.set_value("Metal Server", server.name, "private_network_mac_address", mac_address)


def sync_vm_gateway_routes(server_name: str, reported: object) -> None:
	"""Converge the WireGuard gateway routes that the host's VMs hold.

	Only a VM that turned on the flag holds gateway routes, so the sync
	updates those VMs and skips the rest. The routes stay in the VM
	namespace on the host, so the routes inside a VM never change.
	"""
	from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
		active_gateway_routes,
	)
	from atlas.vm.core.vm_service import VirtualMachineService

	desired = active_gateway_routes()
	for name, routes in get_reported_routes(reported).items():
		current = [route for route in routes if route.is_wireguard_gateway]
		if not current or set(current) == set(desired):
			continue
		if not is_route_sync_ready(name):
			continue
		try:
			VirtualMachineService(frappe.get_doc("Virtual Machine", name)).sync_gateway_routes(desired)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Virtual Machine gateway route sync failed on {server_name}: {name}")


def is_route_sync_ready(name: str) -> bool:
	"""Report whether the sync may replace the gateway routes of one VM."""
	row = frappe.db.get_value(
		"Virtual Machine",
		name,
		["is_draft", "is_terminating", "active_migration"],
		as_dict=True,
	)
	return bool(row) and not row.is_draft and not row.is_terminating and not row.active_migration


def get_reported_routes(reported: object) -> dict[str, list[Route]]:
	"""Return the desired routes of each virtual machine in a Metal sync response."""
	if not isinstance(reported, dict):
		raise ValueError("Metal virtual machine response must be an object")

	routes: dict[str, list[Route]] = {}
	for name, report in reported.items():
		entries = report.get("routes") if isinstance(report, dict) else None
		if not isinstance(entries, list):
			raise ValueError("Metal virtual machine response has invalid values")
		routes[name] = [Route.from_value(entry) for entry in entries]
	return routes


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
	"""Return the managed WireGuard peers for one host.

	An endpoint uses the private address. Every Metal Server in a region shares
	one private network, which is faster than the public uplink and is not
	metered, and the mesh carries every migration byte.
	"""
	servers = frappe.get_all(
		"Metal Server",
		filters={"status": "Running", "is_provisioning_completed": 1},
		fields=[
			"name",
			"wireguard_public_key",
			"wireguard_ip_address",
			"public_ipv4_address",
			"private_ipv4_address",
			"port",
			"private_network_mac_address",
		],
	)
	peers = []
	for server in servers:
		# The mesh needs the public address and the MAC to reach and identify a peer.
		if not all(
			(
				server.wireguard_public_key,
				server.wireguard_ip_address,
				server.private_ipv4_address,
				server.public_ipv4_address,
				server.private_network_mac_address,
			)
		):
			continue

		peers.append(
			{
				"node": server.name,
				"mesh_address": server.wireguard_ip_address,
				"public_key": server.wireguard_public_key,
				"address": f"{server.private_ipv4_address}:{server.port}",
				"public_address": server.public_ipv4_address,
				"private_address": server.private_ipv4_address,
				"private_network_mac_address": server.private_network_mac_address,
			}
		)
	return peers


def is_unicast_network_enabled() -> bool:
	"""Report whether the region uses the unicast NDP transport."""
	return bool(frappe.get_single("Atlas Settings").is_unicast_network_enabled)


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
