from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
	get_wireguard_gateway_routes,
)
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalHostCompatibility
from atlas.vm.core.vm_state import report_invalid_virtual_machines, store_reported_states

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

USAGE_RETENTION = timedelta(hours=3)
# Host sync runs every 10 seconds, so a host outage logs its failure once an hour.
SYNC_FAILURE_REPEAT_SECONDS = 3600


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
	server: MetalServer = frappe.get_doc("Metal Server", server_name)
	try:
		response = MetalClient(server).sync(
			wireguard_peers,
			get_desired_images(),
			privileged_vm_addresses,
			get_wireguard_gateway_routes(),
			unicast,
		)
		values = get_usage_values(response.get("capacity"))
		store_reported_states(server_name, response.get("virtual_machines"))
		report_invalid_virtual_machines(server_name, response.get("invalid_virtual_machines") or [])
	except MetalClientError:
		log_sync_failure(server.name, f"Metal synchronization failed for Server {server.name}")
		return
	except ValueError:
		log_sync_failure(server.name, f"Invalid synchronization response from Server {server.name}")
		return

	frappe.cache.delete_value(sync_failure_key(server.name))
	frappe.get_doc({"doctype": "Metal Server Usage", "server": server.name, **values}).insert(
		ignore_permissions=True
	)

	mac_address = response.get("private_network_mac_address")
	if mac_address and mac_address != server.private_network_mac_address:
		frappe.db.set_value("Metal Server", server.name, "private_network_mac_address", mac_address)

	compatibility = response.get("host_compatibility")
	try:
		if compatibility is not None:
			compatibility = MetalHostCompatibility.from_dict(compatibility)
	except ValueError:
		frappe.log_error(
			frappe.get_traceback(), f"Invalid host compatibility report from Server {server.name}"
		)
		compatibility = None
	report = frappe.as_json(asdict(compatibility)) if compatibility else None
	scaling = bool(compatibility and compatibility.tsc_scaling)
	if (server.host_compatibility_report or None) != report or bool(server.tsc_scaling_supported) != scaling:
		frappe.db.set_value(
			"Metal Server",
			server.name,
			{"host_compatibility_report": report, "tsc_scaling_supported": scaling},
			update_modified=False,
		)
	# A host that cannot run the built-in CPU template already boots its VMs without one.
	if compatibility and compatibility.default_cpu_template_error and not server.is_cpu_template_disabled:
		frappe.db.set_value("Metal Server", server.name, "is_cpu_template_disabled", 1, update_modified=False)


def log_sync_failure(server_name: str, title: str) -> None:
	"""Log a failed sync once until the host syncs again or an hour passes."""
	key = sync_failure_key(server_name)
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=SYNC_FAILURE_REPEAT_SECONDS)
	frappe.log_error(frappe.get_traceback(), title)


def sync_failure_key(server_name: str) -> str:
	return f"atlas:server-sync-failure:{server_name}"


def get_desired_images() -> list[dict[str, Any]]:
	"""Return images that each host must retain locally."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={"enabled": 1, "status": "Available", "cache_image": 1},
		pluck="name",
	)
	images: list[VirtualMachineImage] = [frappe.get_doc("Virtual Machine Image", name) for name in names]
	return [image.get_desired_image() for image in images]


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
