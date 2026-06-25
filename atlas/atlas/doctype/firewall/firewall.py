import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.ssh import run_task

# Identity is frozen once written; the rules and the enabled toggle stay editable.
IMMUTABLE_AFTER_INSERT = ("virtual_machine", "server", "tenant")

PROTOCOLS = ("tcp", "udp")


class Firewall(Document):
	def before_insert(self) -> None:
		self._derive_from_virtual_machine()
		self._assert_one_per_virtual_machine()

	def _derive_from_virtual_machine(self) -> None:
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		self.server = vm.server
		self.tenant = vm.tenant

	def _assert_one_per_virtual_machine(self) -> None:
		"""One firewall per VM (the per-VM model): a VM's public surface is a single
		set of allowed ports, not several overlapping ones. The real guarantee is the
		`unique` index on `virtual_machine` (firewall.json) — this check only turns the
		race-safe DB constraint into a friendly message on the common, non-racing path."""
		if frappe.db.exists("Firewall", {"virtual_machine": self.virtual_machine}):
			frappe.throw(_("A Firewall already exists for {0}").format(self.virtual_machine))

	def validate(self) -> None:
		self._validate_rules()
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			if old_value and old_value != getattr(self, field):
				frappe.throw(_("{0} is immutable after insert").format(field))

	def _validate_rules(self) -> None:
		"""Ports in range, protocols known, no duplicate proto/port row (a duplicate
		would just be a redundant nft accept, but it signals a mistake)."""
		seen: set[tuple[str, int]] = set()
		for rule in self.rules:
			if rule.protocol not in PROTOCOLS:
				frappe.throw(_("Rule protocol must be one of {0}").format(", ".join(PROTOCOLS)))
			if not 1 <= (rule.port or 0) <= 65535:
				frappe.throw(_("Rule port {0} is out of range 1-65535").format(rule.port))
			key = (rule.protocol, rule.port)
			if key in seen:
				frappe.throw(_("Duplicate rule {0}/{1}").format(rule.protocol, rule.port))
			seen.add(key)

	def on_trash(self) -> None:
		"""Deleting the firewall must open the VM back up, or its nft block would
		outlive the row (orphaned, still blocking). Skipped for a terminated VM, whose
		host state vm-network-down already tore down."""
		self._clear_on_host()
		self.db_set("status", "Disabled")

	@frappe.whitelist()
	def sync(self) -> str:
		"""Push the declared state to the host: `enabled` → apply the rules (an empty
		list is a valid deny-all-public), disabled → clear them (VM reverts to public).
		The explicit apply verb, like VPN Tunnel.bring_up — never fired on save, so a
		plain edit does not SSH. Returns the dispatched Task name, or "" when skipped
		(terminated VM, whose host state is already gone)."""
		if self._virtual_machine_terminated():
			self.db_set("status", "Disabled")
			return ""
		task = self._apply_on_host() if self.enabled else self._clear_on_host()
		self.db_set("status", "Active" if self.enabled else "Disabled")
		return task

	def _apply_on_host(self) -> str:
		"""Run firewall-apply.py --action apply: write the durable sidecar and install
		the nft public_filter block."""
		return run_task(
			server=self.server,
			script="firewall-apply.py",
			variables={
				"VIRTUAL_MACHINE_NAME": self.virtual_machine,
				"ACTION": "apply",
				"RULE": [f"{rule.protocol}/{rule.port}" for rule in self.rules],
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		).name

	def _clear_on_host(self) -> str:
		"""Run firewall-apply.py --action clear: remove the nft block + sidecar so the
		VM is fully public again. Skipped for a terminated VM (host already torn down)."""
		if self._virtual_machine_terminated():
			return ""
		return run_task(
			server=self.server,
			script="firewall-apply.py",
			variables={
				"VIRTUAL_MACHINE_NAME": self.virtual_machine,
				"ACTION": "clear",
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		).name

	def _virtual_machine_terminated(self) -> bool:
		return frappe.db.get_value("Virtual Machine", self.virtual_machine, "status") == "Terminated"
