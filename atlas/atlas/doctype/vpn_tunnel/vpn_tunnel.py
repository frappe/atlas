import uuid

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.networking import (
	allocate_tunnel_slot,
	derive_tunnel_interface,
	tunnel_listen_port,
	tunnel_overlay_link,
)
from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# Identity and the WireGuard parameters the host applied — frozen once written.
# transport is locked too: a tunnel's endpoint family is fixed for its lifetime.
IMMUTABLE_AFTER_INSERT = (
	"virtual_machine",
	"server",
	"tenant",
	"transport",
	"client_public_key",
	"slot_index",
	"listen_port",
	"interface_name",
	"client_address",
)

TRANSPORT_PUBLIC_IPV4 = "public-ipv4"


class VPNTunnel(Document):
	def autoname(self) -> None:
		# UUID-named like Virtual Machine: derive_tunnel_interface parses the name
		# as a UUID, and a stable per-tunnel id needs no allocator.
		self.name = str(uuid.uuid4())

	def before_insert(self) -> None:
		# Name-independent derivations (the VM link, the slot). interface_name needs
		# self.name, set by autoname AFTER before_insert, so it is derived in
		# before_validate — mirroring how Virtual Machine derives mac/tap.
		self._derive_from_virtual_machine()
		self._set_defaults()
		self._allocate_slot()

	def _derive_from_virtual_machine(self) -> None:
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		self.server = vm.server
		self.tenant = vm.tenant

	def _set_defaults(self) -> None:
		if not self.status:
			self.status = "Pending"
		if not self.transport:
			self.transport = TRANSPORT_PUBLIC_IPV4

	def _allocate_slot(self) -> None:
		slot = allocate_tunnel_slot(self.server)
		self.slot_index = slot
		self.listen_port = tunnel_listen_port(slot)
		# The client end of the /127 overlay — the client's wg interface address and
		# the host peer's allowed-ip. The host end is recomputed at dispatch.
		_, client_cidr = tunnel_overlay_link(slot)
		self.client_address = client_cidr.split("/", 1)[0]

	def before_validate(self) -> None:
		if not self.is_new():
			return
		if not self.interface_name:
			self.interface_name = derive_tunnel_interface(self.name)

	def validate(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			if old_value and old_value != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def bring_up(self) -> str:
		"""Run vm-tunnel.py --action up on the host: mint/reuse the host key, write
		the durable sidecars, apply the wg interface + nft isolation. Store the
		returned host public key and mark Active. run_task raises on failure, so the
		row only goes Active once the host actually applied the tunnel."""
		if self.status == "Revoked":
			frappe.throw(_("Cannot bring up a revoked tunnel"))
		# The host end of the overlay /127 (index 0); the client end was stored at
		# insert. Indexed, not unpacked, so the throwaway doesn't shadow gettext `_`.
		host_cidr = tunnel_overlay_link(self.slot_index)[0]
		task = run_task(
			server=self.server,
			script="vm-tunnel.py",
			variables={
				"TUNNEL_NAME": self.name,
				"VIRTUAL_MACHINE_NAME": self.virtual_machine,
				"INTERFACE": self.interface_name,
				"ACTION": "up",
				"LISTEN_PORT": str(self.listen_port),
				"CLIENT_PUBLIC_KEY": self.client_public_key,
				"CLIENT_ADDRESS": self.client_address,
				"HOST_ADDRESS": host_cidr,
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		)
		self.server_public_key = parse_result(task.stdout)["server_public_key"]
		self.status = "Active"
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def client_config(self) -> dict:
		"""The ready-to-use client payload (host public key, endpoint, AllowedIPs,
		overlay address, copy-paste `config`, setup steps) for an Active tunnel.

		Only meaningful once the host has minted its key on bring-up, so it is
		guarded on Active. Lazy import to avoid a controller↔api cycle at load."""
		if self.status != "Active":
			frappe.throw(_("Client config is only available once the tunnel is Active"))
		from atlas.atlas.api.tunnel import _client_config

		return _client_config(self)

	@frappe.whitelist()
	def revoke(self) -> str:
		"""Tear the tunnel down on the host and mark Revoked, releasing its slot.

		Always runs the host down Task — UNLIKE Reserved IP.detach, which skips a
		Terminated VM. terminate-vm.py tears down the VM's netns/veth but the wg
		interface lives in the host ROOT netns and survives that, so the down Task is
		what removes it (the VM row still exists — VMs are never deleted)."""
		if self.status == "Revoked":
			frappe.throw(_("Tunnel is already revoked"))
		task = run_task(
			server=self.server,
			script="vm-tunnel.py",
			variables={
				"TUNNEL_NAME": self.name,
				"VIRTUAL_MACHINE_NAME": self.virtual_machine,
				"INTERFACE": self.interface_name,
				"ACTION": "down",
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		)
		self.status = "Revoked"
		self.save(ignore_permissions=True)
		return task.name
