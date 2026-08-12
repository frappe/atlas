# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
import frappe
from atlas.atlas.ssh import connection_for_guest
from atlas.atlas._ssh.transport import Connection, run_ssh, ssh_key_file


class GarageSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from atlas.atlas.doctype.garage_virtual_machines.garage_virtual_machines import GarageVirtualMachines
		from frappe.types import DF

		admin_secret: DF.Data
		api_domain: DF.Data
		data_machines: DF.Table[GarageVirtualMachines]
		gateway_machines: DF.Table[GarageVirtualMachines]
		metrics_secret: DF.Data
		num_nodes: DF.Int
		rpc_secret: DF.Data
		web_domain: DF.Data
	# end: auto-generated types

	_DOCTYPE_NAME = "Garage Settings"
	@frappe.whitelist()
	def apply_layout(self) -> str:
		if self.num_nodes <= 0 or len(self.data_machines) < self.num_nodes:
			frappe.throw("data machines are still less than num of required nodes")

		machine = self.data_machines[0]

		vm = frappe.get_doc("Virtual Machine", machine.virtual_machine)

		base = connection_for_guest(vm)
		connection = Connection(
			host=base.host,
			ssh_private_key=base.ssh_private_key,
			user=base.user,
			port=22,
		)

		command = r'''
	CMD=$(garage layout show | sed -n 's/^[[:space:]]*\(garage layout apply --version .*\)$/\1/p')
	if [ -n "$CMD" ]; then
		eval "$CMD"
	else
		echo "No staged layout changes found"
	fi
	'''

		with ssh_key_file(connection.ssh_private_key) as key_path:
			stdout, stderr, code = run_ssh(
				connection,
				key_path,
				command,
				timeout_seconds=60,
			)

		if code != 0:
			frappe.throw(
				f"Applying garage layout failed (exit {code}): {stderr[-500:]}"
			)

		return stdout or "Layout applied successfully"
	@frappe.whitelist()
	def reconfigure_all_garages(self) -> str:
		"""Reconfigure all garages"""
		from atlas.atlas.garage import configure_garage

		for i in self.data_machines:
			configure_garage(i.virtual_machine)
		for i in self.gateway_machines:
			configure_garage(i.virtual_machine)
		self.apply_layout()
		return "successfully reconfigured all garages"
