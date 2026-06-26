import frappe
from frappe.model.document import Document

# The routing key is the identity (autoname field:subdomain) and the target VM
# is fixed once chosen — repointing a live subdomain at a different VM is a
# delete-and-recreate, not an in-place edit, so the proxy map change is explicit.
IMMUTABLE_AFTER_INSERT = (
	"subdomain",
	"virtual_machine",
)


class Subdomain(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		active: DF.Check
		address: DF.Data
		subdomain: DF.Data
		virtual_machine: DF.Link
	# end: auto-generated types

	def validate(self) -> None:
		self._validate_immutability()
		self._denormalize_address()

	def after_insert(self) -> None:
		"""Auto-reconcile: a new active mapping changes the region's served map, so
		push it to the fleet — the operator never has to run a reconcile by hand
		after creating a subdomain (mirrors VirtualMachine.after_insert)."""
		self._enqueue_reconcile()

	def on_update(self) -> None:
		"""The routing key and target VM are immutable, so `active` is the only
		mutable field that changes the served map. Reconcile only when it actually
		flipped — a no-op save shouldn't SSH the whole fleet."""
		original = self.get_doc_before_save()
		if original and original.active != self.active:
			self._enqueue_reconcile()

	def on_trash(self) -> None:
		"""Deleting an active mapping drops it from the served map; reconcile so the
		proxy fleet stops routing the subdomain."""
		self._enqueue_reconcile()

	def _enqueue_reconcile(self) -> None:
		"""Background-reconcile the proxy fleet. queue=long because the job SSHes into
		every proxy (slow); reconcile_proxies tolerates an empty fleet (no-op) and
		isolates per-proxy failures, so a missing or wedged proxy never fails the
		operator's save.

		Deduplicated: a reconcile reads the WHOLE desired map (`subdomain_map`), so it
		is the same job no matter which subdomain triggered it — N subdomain changes
		need one reconcile, not N. Without this, a burst of changes (an e2e, a bulk
		edit) floods `long` with identical jobs; with a wedged or missing proxy each
		takes its full SSH timeout, so they pile up far faster than they drain and
		starve every other `long` job (observed: 4000+ redundant reconciles backing up
		the queue while a proxy was down). `deduplicate=True` with a constant `job_id`
		collapses the burst to a single queued reconcile; `enqueue_after_commit` so the
		job sees this change's committed map row."""
		frappe.enqueue(
			"atlas.atlas.doctype.subdomain.subdomain.auto_reconcile",
			queue="long",
			timeout=300,
			job_id="auto_reconcile_subdomains",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def _validate_immutability(self) -> None:
		"""Lock the routing key and its target VM once written. The `address` is the
		one mutable field (it tracks the VM's ipv6), and `active` toggles the mapping
		in/out of the served map."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	def _denormalize_address(self) -> None:
		"""Copy the target VM's public IPv6 onto `address`, so the desired-map
		query (subdomain_map) is a single SELECT with no join. The proxy dials
		this literal; it never resolves a VM. A VM with no ipv6 yet is a hard
		error — an unaddressable target can't be a routing destination."""
		address = frappe.db.get_value("Virtual Machine", self.virtual_machine, "ipv6_address")
		if not address:
			frappe.throw(
				f"Virtual Machine {self.virtual_machine} has no ipv6_address; cannot map a subdomain to it"
			)
		self.address = address


def subdomain_map() -> dict[str, str]:
	"""The desired subdomain→address map: every ACTIVE subdomain. This is the full
	map every proxy VM serves (the design's "each proxy holds the whole map",
	spec/12-proxy.md).

	The proxy reconcile (atlas.atlas.proxy) compares this, serialized canonically,
	against each proxy guest's live `/map` and bulk-`/sync`s on drift."""
	rows = frappe.get_all(
		"Subdomain",
		filters={"active": 1},
		fields=["subdomain", "address"],
	)
	return {row["subdomain"]: row["address"] for row in rows}


def auto_reconcile() -> None:
	"""Background-job entrypoint. Enqueued by Subdomain's insert/active-toggle/
	delete hooks so a mapping change reaches the proxy fleet without the operator
	running a reconcile. Thin wrapper over atlas.atlas.proxy.reconcile_proxies —
	kept here (not as a direct enqueue of proxy.reconcile_proxies) so the Subdomain
	module owns its own background verb and the import stays lazy."""
	from atlas.atlas.proxy import reconcile_proxies

	reconcile_proxies()
