from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import frappe

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.vm_service import VirtualMachineService

if TYPE_CHECKING:
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


@run_as_admin
def reconcile_stale_draft(name: str) -> None:
	"""Finalize a draft that Metal holds, or delete one that Metal does not."""
	settle(name, "draft reconciliation", on_present=lambda machine: machine.db_set("is_draft", 0))


@run_as_admin
def reconcile_terminating(name: str) -> None:
	"""Delete a terminating virtual machine after Metal confirms its absence."""
	settle(name, "termination reconciliation", on_present=None)


def settle(
	name: str,
	description: str,
	on_present: Callable[[VirtualMachine], None] | None,
) -> None:
	"""Settle one Atlas record against Metal. Metal is the authority."""
	virtual_machine = cast("VirtualMachine", frappe.get_doc("Virtual Machine", name))

	try:
		VirtualMachineService(virtual_machine).metal_client.get_virtual_machine(name)
	except MetalClientError as error:
		if not error.is_not_found:
			log_failure(name, description)
			return

		delete_virtual_machine(virtual_machine, description)
		return

	if on_present:
		on_present(virtual_machine)


def delete_virtual_machine(virtual_machine: VirtualMachine, description: str) -> None:
	"""Delete one absent record. A failure is logged and does not stop the batch."""
	virtual_machine.flags.metal_absence_confirmed = True

	try:
		virtual_machine.delete()
		frappe.db.commit()  # nosemgrep
	except Exception:
		frappe.db.rollback()
		log_failure(cast(str, virtual_machine.name), description)


def log_failure(name: str, description: str) -> None:
	"""Record why one virtual machine did not settle."""
	frappe.log_error(
		message=frappe.get_traceback(),
		title=f"Virtual Machine {name} {description} failed",
	)
