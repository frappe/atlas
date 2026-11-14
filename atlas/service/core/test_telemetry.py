from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

import atlas.service.core.telemetry as telemetry
from atlas.atlas.core.ssh import SSHResult
from atlas.service.core.telemetry import ServiceTelemetry


class TestServiceTelemetry(UnitTestCase):
	def install(self, doctype: str) -> Mock:
		virtual_machine = SimpleNamespace(
			name="vm-00001", ssh_host="vm-00001.internal", get_ssh_proxy_command=Mock(return_value=None)
		)
		agent = Mock()
		agent.return_value.install.return_value = SSHResult(exit_code=0, output="")
		with (
			patch.object(telemetry.frappe, "get_doc", return_value=virtual_machine),
			patch.object(telemetry, "SSHRunner"),
			patch.object(telemetry, "DatumAgent", agent),
		):
			ServiceTelemetry(
				SimpleNamespace(doctype=doctype, name="node", virtual_machine="vm-00001")
			).configure()
		return agent

	def test_a_service_node_sends_as_atlas_with_its_role(self) -> None:
		agent = self.install("Proxy Server")

		self.assertEqual(agent.call_args.args[1:], ("vm-00001", "atlas"))
		agent.return_value.install.assert_called_once_with(role="proxy")

	def test_the_cargo_vm_sends_as_cargo_without_a_role(self) -> None:
		agent = self.install("Cargo Server")

		self.assertEqual(agent.call_args.args[1:], ("vm-00001", "cargo"))
		agent.return_value.install.assert_called_once_with(role=None)
