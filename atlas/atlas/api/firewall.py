"""The per-VM firewall API — a VM owner (or Central) sets which public ports are
open (spec/20-firewall.md).

The user-facing write half of the firewall, mirroring `tunnel.request_tunnel`: it
is owner-scoped and Central-callable as the service user (the caller must be an
operator or own the VM), runs with `ignore_permissions` after the explicit access
check, and reconciles the one per-VM `Firewall` row before pushing it to the host.

The VPN tunnel always bypasses the firewall (full access); this only governs the
public surface. An empty rule set is a valid deny-all-public (VPN-only) firewall.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.permissions import _is_operator


@frappe.whitelist()
def set_firewall(
	virtual_machine: str,
	rules: list | str | None = None,
	enabled: bool = True,
	label: str | None = None,
) -> dict:
	"""Create or update the VM's firewall and apply it on the host.

	`rules` is a list of `{"protocol": "tcp"|"udp", "port": int}` (or `"tcp/443"`
	tokens), JSON-encoded when called over HTTP. An empty/omitted list with
	`enabled` is a deny-all-public firewall."""
	_assert_can_access_vm(virtual_machine)
	parsed = _parse_rules(rules)

	name = frappe.db.get_value("Firewall", {"virtual_machine": virtual_machine})
	firewall = frappe.get_doc("Firewall", name) if name else frappe.new_doc("Firewall")
	if not name:
		firewall.virtual_machine = virtual_machine
	if label is not None:
		firewall.label = label
	firewall.enabled = 1 if frappe.utils.sbool(enabled) else 0
	firewall.set("rules", [{"protocol": protocol, "port": port} for (protocol, port) in parsed])
	firewall.save(ignore_permissions=True)  # validates rules + immutability
	firewall.sync()  # apply (or clear, if disabled) on the host
	return _state(firewall)


@frappe.whitelist()
def get_firewall(virtual_machine: str) -> dict | None:
	"""The VM's current firewall, or None if it has none (fully public)."""
	_assert_can_access_vm(virtual_machine)
	name = frappe.db.get_value("Firewall", {"virtual_machine": virtual_machine})
	return _state(frappe.get_doc("Firewall", name)) if name else None


@frappe.whitelist()
def remove_firewall(virtual_machine: str) -> dict:
	"""Delete the VM's firewall, reopening it to the public internet. The row's
	on_trash clears the nft block on the host. A no-op if none exists."""
	_assert_can_access_vm(virtual_machine)
	name = frappe.db.get_value("Firewall", {"virtual_machine": virtual_machine})
	if name:
		frappe.get_doc("Firewall", name).delete(ignore_permissions=True)
	return {"virtual_machine": virtual_machine, "status": "Disabled", "enabled": False, "rules": []}


def _assert_can_access_vm(virtual_machine: str) -> None:
	"""Operator, or the VM's owner — the `tunnel.request_tunnel` access model."""
	if not frappe.db.exists("Virtual Machine", virtual_machine):
		frappe.throw(_("Virtual Machine {0} not found").format(virtual_machine))
	user = frappe.session.user
	if _is_operator(user):
		return
	if frappe.db.get_value("Virtual Machine", virtual_machine, "owner") != user:
		raise frappe.PermissionError(_("You do not have access to this Virtual Machine"))


def _parse_rules(rules) -> list[tuple[str, int]]:
	"""Normalize the `rules` argument (JSON string, list of dicts, or proto/port
	tokens) to `(protocol, port)` tuples. Shape only — the Firewall row's validate()
	is the single source of truth for valid protocols and port ranges."""
	if not rules:
		return []
	if isinstance(rules, str):
		rules = frappe.parse_json(rules)
	parsed: list[tuple[str, int]] = []
	for rule in rules:
		if isinstance(rule, str):
			protocol, _, port = rule.partition("/")
		else:
			protocol, port = rule.get("protocol"), rule.get("port")
		parsed.append((protocol, int(port)))
	return parsed


def _state(firewall) -> dict:
	"""The firewall's public shape: identity, host status, and the allowed ports."""
	return {
		"name": firewall.name,
		"virtual_machine": firewall.virtual_machine,
		"server": firewall.server,
		"enabled": bool(firewall.enabled),
		"status": firewall.status,
		"rules": [f"{rule.protocol}/{rule.port}" for rule in firewall.rules],
	}
