import frappe
from frappe.model.document import Document

# The routing key is the identity (autoname field:subdomain) and the target VM
# is fixed once chosen — repointing a live subdomain at a different VM is a
# delete-and-recreate, not an in-place edit, so the proxy map change is explicit.
IMMUTABLE_AFTER_INSERT = (
	"subdomain",
	"virtual_machine",
	"region",
)


class Subdomain(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		active: DF.Check
		address: DF.Data
		region: DF.Data
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
		"""The routing key, target VM, and region are immutable, so `active` is the
		only mutable field that changes the served map. Reconcile only when it
		actually flipped — a no-op save shouldn't SSH the whole fleet."""
		original = self.get_doc_before_save()
		if original and original.active != self.active:
			self._enqueue_reconcile()

	def on_trash(self) -> None:
		"""Deleting an active mapping drops it from the served map; reconcile so the
		proxy fleet stops routing the subdomain."""
		self._enqueue_reconcile()

	def _enqueue_reconcile(self) -> None:
		"""Background-reconcile this subdomain's region. queue=long because the job
		SSHes into every proxy in the region (slow); reconcile_region tolerates an
		empty fleet (no-op) and isolates per-proxy failures, so a missing or wedged
		proxy never fails the operator's save.

		Deduplicated per region: a reconcile reads the WHOLE region's desired map
		(`map_for_region`), so it is the same job no matter which subdomain triggered
		it — N subdomain changes need one reconcile, not N. Without this, a burst of
		changes (an e2e, a bulk edit) floods `long` with identical jobs; with a wedged
		or missing proxy each takes its full SSH timeout, so they pile up far faster
		than they drain and starve every other `long` job (observed: 4000+ redundant
		reconciles backing up the queue while a proxy was down). `deduplicate=True`
		with a region-keyed `job_id` collapses the burst to a single queued reconcile;
		`enqueue_after_commit` so the job sees this change's committed map row."""
		frappe.enqueue(
			"atlas.atlas.doctype.subdomain.subdomain.auto_reconcile_region",
			queue="long",
			timeout=300,
			job_id=f"auto_reconcile_region::{self.region}",
			deduplicate=True,
			enqueue_after_commit=True,
			region=self.region,
		)

	def _validate_immutability(self) -> None:
		"""Lock the routing key, its target VM, and its region once written. The
		`address` is the one mutable field (it tracks the VM's ipv6), and `active`
		toggles the mapping in/out of the served map."""
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
		query (map_for_region) is a single SELECT with no join. The proxy dials
		this literal; it never resolves a VM. A VM with no ipv6 yet is a hard
		error — an unaddressable target can't be a routing destination."""
		address = frappe.db.get_value("Virtual Machine", self.virtual_machine, "ipv6_address")
		if not address:
			frappe.throw(
				f"Virtual Machine {self.virtual_machine} has no ipv6_address; cannot map a subdomain to it"
			)
		self.address = address


def map_for_region(region: str) -> dict[str, str]:
	"""The desired subdomain→address map for a region: every ACTIVE subdomain in
	the region. This is the full map every proxy VM in the region serves (the
	design's "each proxy holds the whole regional map", spec/12-proxy.md).

	The proxy reconcile (atlas.atlas.proxy) compares this, serialized canonically,
	against each proxy guest's live `/map` and bulk-`/sync`s on drift."""
	rows = frappe.get_all(
		"Subdomain",
		filters={"region": region, "active": 1},
		fields=["subdomain", "address"],
	)
	return {row["subdomain"]: row["address"] for row in rows}


def auto_reconcile_region(region: str) -> None:
	"""Background-job entrypoint. Enqueued by Subdomain's insert/active-toggle/
	delete hooks so a mapping change reaches the proxy fleet without the operator
	running a reconcile. Thin wrapper over atlas.atlas.proxy.reconcile_region —
	kept here (not as a direct enqueue of proxy.reconcile_region) so the Subdomain
	module owns its own background verb and the import stays lazy."""
	from atlas.atlas.proxy import reconcile_region

	reconcile_region(region)
