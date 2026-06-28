import frappe
from frappe.model.document import Document

from atlas.atlas.providers import for_provider_type
from atlas.atlas.ssh import run_task

# The IP belongs to the Server for its lifetime; only the VM attachment moves.
IMMUTABLE_AFTER_INSERT = (
	"ip_address",
	"server",
	"provider_resource_id",
)


class ReservedIP(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		ip_address: DF.Data
		provider_resource_id: DF.Data | None
		server: DF.Link
		status: DF.Literal["Allocated", "Attached"]
		virtual_machine: DF.Link | None
	# end: auto-generated types

	def validate(self) -> None:
		self._validate_immutability()
		self._sync_status()

	def _validate_immutability(self) -> None:
		"""Lock the IP, its Server, and the vendor handle once written. Allow the
		initial None → value population (the same idiom as Server)."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			if old_value and old_value != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	def _sync_status(self) -> None:
		"""status is derived from virtual_machine: Attached iff a VM is set."""
		self.status = "Attached" if self.virtual_machine else "Allocated"

	@frappe.whitelist()
	def attach(self, virtual_machine: str) -> None:
		"""Attach this Server-allocated IP to a VM on the same Server.

		Three effects, in failure-safe order: bind the reserved IP to the droplet
		at the vendor, run the host-side 1:1-NAT Task (the inbound mirror of the
		NAT44 egress — DNAT the reserved v4 in, SNAT the guest's egress out as it),
		then commit the Frappe invariant (one IP, one VM, same Server) and
		denormalize the address onto Virtual Machine.public_ipv4. The vendor bind
		and the Task both raise on failure, so a half-applied attach never leaves a
		Frappe row claiming an attachment the host doesn't have."""
		if self.virtual_machine:
			frappe.throw(f"{self.ip_address} is already attached to {self.virtual_machine}")
		vm = frappe.get_doc("Virtual Machine", virtual_machine)
		if vm.server != self.server:
			frappe.throw(f"{self.ip_address} is allocated to a different Server than {virtual_machine}")
		if vm.public_ipv4:
			frappe.throw(f"{virtual_machine} already has a public IPv4 ({vm.public_ipv4})")

		# 1. Bind at the vendor: the reserved IP attaches to the DROPLET (the
		#    Server's provider_resource_id), not the guest — DO has no API to bind
		#    it to a Firecracker VM. Idempotent: DO no-ops a re-assign. Self-Managed
		#    is a no-op (the operator routes it). Same Server as discover() maps by.
		if self.provider_resource_id:
			droplet_id = frappe.db.get_value("Server", self.server, "provider_resource_id")
			if not droplet_id:
				frappe.throw(f"Server {self.server} has no provider_resource_id; cannot assign reserved IP")
			_provider_for_server(self.server).assign_reserved_ip(self.provider_resource_id, droplet_id)
		# 2. Host 1:1-NAT, live (no reboot). vm-reserved-ip.py also writes
		#    RESERVED_IPV4 into network.env so a later boot re-creates the NAT.
		self._run_nat_task(vm, "attach")
		# 3. Commit the Frappe invariant last.
		self.virtual_machine = virtual_machine
		self.save()
		vm.db_set("public_ipv4", self.ip_address)

	@frappe.whitelist()
	def detach(self) -> None:
		"""Release this IP from its VM, leaving it allocated to the Server and
		available to attach elsewhere. Tears down the host 1:1-NAT and unbinds the
		IP at the vendor, then clears the Frappe invariant and the VM's
		denormalized address.

		The host NAT Task is **skipped for a Terminated VM**: terminate-vm.py has
		already `rm -rf`'d the VM directory (network.env included) and its
		ExecStopPost (vm-network-down.py) already removed the NAT — running the
		Task would only fail reading a deleted env. A Stopped VM keeps its env, so
		the Task still runs there to clear RESERVED_IPV4 (its `remove()` is a no-op
		on rules that aren't live). A VM row that is fully gone is tolerated too."""
		if not self.virtual_machine:
			frappe.throw(f"{self.ip_address} is not attached to any VM")
		vm_name = self.virtual_machine
		vm = (
			frappe.get_doc("Virtual Machine", vm_name)
			if frappe.db.exists("Virtual Machine", vm_name)
			else None
		)
		# 1. Tear down the host NAT, unless the VM is gone or already Terminated
		#    (the terminate path already dropped the host networking + the env).
		if vm and vm.status != "Terminated":
			self._run_nat_task(vm, "detach")
		# 2. Unbind at the vendor so the IP can attach to another droplet later.
		if self.provider_resource_id:
			_provider_for_server(self.server).unassign_reserved_ip(self.provider_resource_id)
		# 3. Clear the Frappe invariant + the denormalized address.
		self.virtual_machine = None
		self.save()
		if vm:
			vm.db_set("public_ipv4", None)

	def _run_nat_task(self, vm, action: str) -> None:
		"""Dispatch vm-reserved-ip.py to add/remove the host 1:1-NAT on the VM's
		Server. One task, one script (spec principle #3); run_task raises on
		failure so the caller's invariant commit is gated on the host change."""
		run_task(
			server=self.server,
			script="vm-reserved-ip",
			variables={
				"VIRTUAL_MACHINE_NAME": vm.name,
				"RESERVED_IPV4": self.ip_address,
				"ACTION": action,
			},
			virtual_machine=vm.name,
			timeout_seconds=60,
		)

	@frappe.whitelist()
	def release(self) -> None:
		"""Destroy the vendor reserved IP and delete this row, returning the
		address to the vendor's pool. The IP must be detached first.

		Explicit, like `Server.archive()` — destroying a vendor resource is
		never a side effect of deleting the Frappe row (see `on_trash`)."""
		if self.virtual_machine:
			frappe.throw(f"Detach {self.ip_address} from {self.virtual_machine} before releasing it")
		if self.provider_resource_id:
			_provider_for_server(self.server).release_reserved_ip(self.provider_resource_id)
		self.delete()

	def on_trash(self) -> None:
		"""Refuse to delete a row whose IP is still attached — that would strand
		the VM's denormalized `public_ipv4` and (later) the host NAT. Deleting
		the row does NOT touch the vendor; use `release()` to destroy the vendor
		IP, mirroring `Server`'s explicit `archive()`."""
		if self.virtual_machine:
			frappe.throw(f"Detach {self.ip_address} from {self.virtual_machine} before deleting it")


def _provider_for_server(server: str):
	"""Resolve the Provider for a Reserved IP via its Server. Reserved IPs are
	per-Server, so we use the Server's own provider_type rather than the globally
	active one (`atlas.get_provider()`) — correct even after a vendor switch."""
	provider_type = frappe.db.get_value("Server", server, "provider_type")
	return for_provider_type(provider_type)


@frappe.whitelist()
def allocate(server: str) -> str:
	"""Reserve a fresh public IPv4 at the vendor for `server` and write a
	`Reserved IP` row for it (Allocated, unattached). Returns the new row name.

	The vendor reserves the IP in its (single) region; binding it to the
	droplet and the host 1:1-NAT happen on `attach()` (a follow-up Task), not
	here — an allocated-but-unattached IP is the resting state of a pool entry."""
	reserved = _provider_for_server(server).allocate_reserved_ip()
	return (
		frappe.get_doc(
			{
				"doctype": "Reserved IP",
				"ip_address": reserved.ip_address,
				"server": server,
				"provider_resource_id": reserved.provider_resource_id,
			}
		)
		.insert()
		.name
	)


@frappe.whitelist()
def discover(server: str) -> list[str]:
	"""Import the vendor's reserved IPs already bound to `server`'s droplet
	into the pool, creating a `Reserved IP` row for each one Atlas doesn't yet
	model. Returns the names of the rows created (existing ones are skipped).

	Reconcile, vendor → Frappe: a reserved IP the operator created out-of-band
	(or one that survived a row deletion) shows up in the pool on the next
	discover. Mapped by `droplet_resource_id` == the Server's
	`provider_resource_id`, so only IPs on *this* host are imported."""
	droplet_id = frappe.db.get_value("Server", server, "provider_resource_id")
	if not droplet_id:
		frappe.throw(f"Server {server} has no provider_resource_id; cannot discover reserved IPs")
	created = []
	for reserved in _provider_for_server(server).list_reserved_ips():
		if reserved.droplet_resource_id != droplet_id:
			continue
		if frappe.db.exists("Reserved IP", {"ip_address": reserved.ip_address}):
			continue
		row = frappe.get_doc(
			{
				"doctype": "Reserved IP",
				"ip_address": reserved.ip_address,
				"server": server,
				"provider_resource_id": reserved.provider_resource_id,
			}
		).insert()
		created.append(row.name)
	return created
