from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

from atlas.api.core.base import (
	ApiResult,
	ListQuery,
	Page,
	build_page,
	get_owned_document,
)
from atlas.api.core.docs import api_docs
from atlas.api.core.errors import (
	ResourceConflict,
)
from atlas.api.models import (
	ComputeUpdatePayload,
	ConsoleTokenPayload,
	ConsoleTokenResponse,
	CreateVirtualMachinePayload,
	DiskUpdatePayload,
	ImageResponse,
	IPAddressAssignmentPayload,
	MetadataReplacementPayload,
	NetworkUpdatePayload,
	SnapshotPayload,
	SSHKeysReplacementPayload,
	VirtualMachineDetailResponse,
	VirtualMachineListResponse,
	VirtualMachineResponse,
)
from atlas.api.router import (
	get_resource_location,
	virtual_machine_actions,
	virtual_machine_configuration,
	virtual_machines,
)
from atlas.api.routes.images import get_owned_image
from atlas.api.routes.ip_addresses import get_owned_ip_address
from atlas.auth.identity import get_current_tenant_id
from atlas.vm.core.console_token import CONSOLE_TOKEN_TTL_SECONDS
from atlas.vm.core.vm_state import get_reported_state_rows
from atlas.vm.doctype.virtual_machine.virtual_machine import create as create_virtual_machine_request

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address import (
		MetalServerIPAddress,
	)
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


ACCEPTED_RESPONSE = {202: {"description": "The change is accepted. Poll the virtual machine route."}}


def get_owned_virtual_machine(virtual_machine_id: str) -> VirtualMachine:
	"""Return one virtual machine that the request tenant owns."""
	return get_owned_document("Virtual Machine", virtual_machine_id)


def request_virtual_machine_power_state(
	virtual_machine_id: str, state: str
) -> ApiResult[VirtualMachineResponse]:
	"""Ask the host for one desired power state."""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.set_power_state(state)
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


def get_available_ip_address(ip_address_id: str) -> MetalServerIPAddress:
	"""Return one reserved IP address that no virtual machine uses."""
	ip_address = get_owned_ip_address(ip_address_id)
	if ip_address.status != "Allocated" or ip_address.virtual_machine:
		raise ResourceConflict("The IP address is not available.")

	return ip_address


@virtual_machines.post("")
@api_docs(
	request_example={
		"image_id": "8f1c2d3e4b5a6978",
		"vcpus": 2,
		"memory_mib": 2048,
		"disk_mib": 20480,
		"hostname": "worker-1",
		"ssh_keys": ["ssh-ed25519 AAAA"],
	},
	responses={201: {"description": "The virtual machine request is stored."}},
)
def create_virtual_machine(
	payload: CreateVirtualMachinePayload,
) -> ApiResult[VirtualMachineResponse]:
	"""Create VM.

	Creates a tenant VM from an image and requests the specified compute, disk, network, and guest configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through the mesh.
	"""
	image = get_owned_image(payload.image_id)
	ip_address = get_available_ip_address(payload.ip_address_id) if payload.ip_address_id else None
	request = payload.to_domain_request(
		get_current_tenant_id(), image.name, ip_address.name if ip_address else None
	)
	result = create_virtual_machine_request(request)
	virtual_machine: VirtualMachine = frappe.get_doc("Virtual Machine", result["name"])

	return ApiResult(
		VirtualMachineResponse.from_document(virtual_machine),
		status=201,
		headers={
			"Location": get_resource_location("virtual-machines", virtual_machine.name),
		},
	)


@virtual_machines.get("")
@api_docs()
def list_virtual_machines(query: ListQuery) -> Page[VirtualMachineListResponse]:
	"""List VMs.

	Returns one page of tenant VM records in newest-first order, with the state each host last reported. This request does not contact the host.
	"""
	rows: list[VirtualMachine] = frappe.get_list(
		"Virtual Machine",
		filters={"tenant_id": get_current_tenant_id()},
		fields=[
			"name",
			"tenant_id",
			"virtual_machine_image",
			"vcpus",
			"memory_mib",
			"disk_mib",
			"sleep_after_idle_seconds",
			"is_draft",
			"is_terminating",
			"creation",
		],
		order_by="creation desc",
		offset=query.offset,
		limit=query.fetch_limit,
	)
	states = get_reported_state_rows([row.name for row in rows])
	return build_page(
		[VirtualMachineListResponse.from_document_and_state(row, states.get(row.name)) for row in rows],
		query,
	)


@virtual_machines.get("<virtual_machine_id>")
@api_docs()
def get_virtual_machine(virtual_machine_id: str) -> VirtualMachineDetailResponse:
	"""Get VM.

	Returns the stored VM record together with its desired state and current state.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	information = virtual_machine.get_metal_vm_info()
	return VirtualMachineDetailResponse.from_document_and_metal(virtual_machine, information)


@virtual_machines.delete("<virtual_machine_id>")
@api_docs(
	responses={202: {"description": "Termination started. Poll the virtual machine route."}},
)
def delete_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Delete VM.

	Starts VM termination and detaches its public IP address without releasing the tenant reservation. Poll the VM until cleanup removes the record and this route returns 404.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.terminate()
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_actions.post("<virtual_machine_id>/actions/start")
@api_docs(
	responses={202: {"description": "The request is accepted. Poll the virtual machine route."}},
)
def start_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Start VM.

	Requests the running state. Poll the VM route to observe completion.
	"""
	return request_virtual_machine_power_state(virtual_machine_id, "running")


@virtual_machine_actions.post("<virtual_machine_id>/actions/stop")
@api_docs(
	responses={202: {"description": "The request is accepted. Poll the virtual machine route."}},
)
def stop_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Stop VM.

	Requests the stopped state. Poll the VM route to observe completion.
	"""
	return request_virtual_machine_power_state(virtual_machine_id, "stopped")


@virtual_machine_actions.post("<virtual_machine_id>/actions/pause")
@api_docs(
	responses={202: {"description": "The request is accepted. Poll the virtual machine route."}},
)
def pause_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Pause VM.

	Pauses the VM without stopping it. Poll the VM route to observe completion.
	"""
	return request_virtual_machine_power_state(virtual_machine_id, "paused")


@virtual_machine_actions.post("<virtual_machine_id>/actions/resume")
@api_docs(
	responses={202: {"description": "The request is accepted. Poll the virtual machine route."}},
)
def resume_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Resume VM.

	Returns a paused VM to the running state. Poll the VM route to observe completion.
	"""
	return request_virtual_machine_power_state(virtual_machine_id, "running")


@virtual_machine_actions.post("<virtual_machine_id>/actions/restart")
@api_docs(
	responses={202: {"description": "The request is accepted. Poll the virtual machine route."}},
)
def restart_virtual_machine(virtual_machine_id: str) -> ApiResult[VirtualMachineResponse]:
	"""Restart VM.

	Requests an in-place restart. Poll the VM route to observe completion.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.reboot()
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_actions.post("<virtual_machine_id>/actions/snapshot")
@api_docs(
	request_example={
		"title": "worker-1 golden",
		"image_type": "machine",
		"cache_image": False,
		"memory_snapshot": False,
	},
	responses={201: {"description": "The Machine image record is created."}},
)
def create_virtual_machine_snapshot(
	virtual_machine_id: str, payload: SnapshotPayload
) -> ApiResult[ImageResponse]:
	"""Create snapshot.

	Creates a reusable Machine image from the current VM disk. The new image belongs to the same tenant.

	If `memory_snapshot` is true, Atlas also records the VM shape for compatible warm starts. Only tenant 0 can set `image_type` to `system`, which shares the image with every tenant, and only tenant 0 can set `cache_image` and `memory_snapshot`. These values cannot change after creation.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	image_name = virtual_machine.create_machine_image(
		payload.title.strip(),
		image_type=payload.image_type,
		cache_image=payload.cache_image,
		memory_snapshot=payload.memory_snapshot,
	)
	image: VirtualMachineImage = frappe.get_doc("Virtual Machine Image", image_name)

	return ApiResult(
		ImageResponse.from_document(image),
		status=201,
		headers={"Location": get_resource_location("images", image.name)},
	)


@virtual_machine_actions.post("<virtual_machine_id>/actions/console-token")
@api_docs(
	request_example={"mode": "tty"},
	responses={200: {"description": "A single-use console token."}},
)
def create_virtual_machine_console_token(
	virtual_machine_id: str, payload: ConsoleTokenPayload
) -> ConsoleTokenResponse:
	"""Create console token.

	Returns a single-use token for the Atlas realtime TTY or SSH console. The token expires after 60 seconds.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	connection = virtual_machine.get_console_token(payload.mode)
	return ConsoleTokenResponse(
		token=connection["token"],
		mode=payload.mode,
		expires_in=CONSOLE_TOKEN_TTL_SECONDS,
	)


@virtual_machine_configuration.patch("<virtual_machine_id>/compute")
@api_docs(
	request_example={"vcpus": 4, "sleep_after_idle_seconds": 1800},
	responses=ACCEPTED_RESPONSE,
)
def update_virtual_machine_compute(
	virtual_machine_id: str, payload: ComputeUpdatePayload
) -> ApiResult[VirtualMachineResponse]:
	"""Update compute.

	Changes the vCPU count, the memory size, and the idle shutdown delay. A vCPU or memory change needs a stopped VM.

	A value of `0` disables automatic idle shutdown.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.update_compute(payload.to_domain_changes())
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.patch("<virtual_machine_id>/disk")
@api_docs(
	request_example={"disk_mib": 40960, "disk_throughput_mibps": 100},
	responses=ACCEPTED_RESPONSE,
)
def update_virtual_machine_disk(
	virtual_machine_id: str, payload: DiskUpdatePayload
) -> ApiResult[VirtualMachineResponse]:
	"""Resize disk.

	Increases the disk size or changes its throughput and IOPS limits. The disk size cannot decrease, and a limit of 0 removes that limit.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.update_disk(payload.to_domain_changes())
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.patch("<virtual_machine_id>/network")
@api_docs(
	request_example={"egress": "uplink", "public_network_throughput_mibps": 50},
	responses=ACCEPTED_RESPONSE,
)
def update_virtual_machine_network(
	virtual_machine_id: str, payload: NetworkUpdatePayload
) -> ApiResult[VirtualMachineResponse]:
	"""Update network.

	Changes egress and network throughput limits. Egress controls internet reachability and does not change mesh reachability.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.update_network(payload.model_dump(exclude_unset=True))
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.put("<virtual_machine_id>/ssh-keys")
@api_docs(
	request_example={"ssh_keys": ["ssh-ed25519 AAAA"]},
	responses=ACCEPTED_RESPONSE,
)
def replace_virtual_machine_ssh_keys(
	virtual_machine_id: str, payload: SSHKeysReplacementPayload
) -> ApiResult[VirtualMachineResponse]:
	"""Replace SSH keys.

	Replaces the complete authorized SSH key list. Keys that are not in the request are removed.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.replace_ssh_keys(payload.ssh_keys)
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.put("<virtual_machine_id>/metadata")
@api_docs(
	request_example={"metadata": {"environment": "production"}},
	responses=ACCEPTED_RESPONSE,
)
def replace_virtual_machine_metadata(
	virtual_machine_id: str, payload: MetadataReplacementPayload
) -> ApiResult[VirtualMachineResponse]:
	"""Replace metadata.

	Replaces the complete custom metadata map. Entries that are not in the request are removed.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	virtual_machine.replace_metadata(payload.metadata)
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.put("<virtual_machine_id>/ip-address")
@api_docs(
	request_example={"ip_address_id": "203.0.113.10"},
	responses=ACCEPTED_RESPONSE,
)
def attach_virtual_machine_ip_address(
	virtual_machine_id: str, payload: IPAddressAssignmentPayload
) -> ApiResult[VirtualMachineResponse]:
	"""Attach IP address.

	Attaches one available IP address that the tenant reserved. Detach the current address before you attach a different one.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	attached_ip_address_name = frappe.db.get_value(
		"Metal Server IP Address", {"virtual_machine": virtual_machine.name}
	)
	if attached_ip_address_name == payload.ip_address_id:
		return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)
	if attached_ip_address_name:
		raise ResourceConflict("Detach the current IP address before you attach a different one.")

	ip_address = get_available_ip_address(payload.ip_address_id)
	virtual_machine.attach_ip_address(ip_address.name)
	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)


@virtual_machine_configuration.delete("<virtual_machine_id>/ip-address")
@api_docs(responses=ACCEPTED_RESPONSE)
def detach_virtual_machine_ip_address(
	virtual_machine_id: str,
) -> ApiResult[VirtualMachineResponse]:
	"""Detach IP address.

	Detaches the public IP address from the VM and keeps its tenant reservation.
	"""
	virtual_machine = get_owned_virtual_machine(virtual_machine_id)
	if frappe.db.exists("Metal Server IP Address", {"virtual_machine": virtual_machine.name}):
		virtual_machine.detach_ip_address()

	return ApiResult(VirtualMachineResponse.from_document(virtual_machine), status=202)
