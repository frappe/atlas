from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.ssh import SSHResult, SSHRunner
from atlas.atlas.core.telemetry.agent import INSTALL_TIMEOUT_SECONDS, ROLES, DatumAgent
from atlas.atlas.core.telemetry.collectors import SERVICE_DOCTYPES


class TestDatumAgentRoles(UnitTestCase):
	def test_each_service_has_a_role_file(self) -> None:
		for service in SERVICE_DOCTYPES.values():
			self.assertTrue((ROLES / f"{service}.yaml").is_file(), service)

	def test_a_role_file_adds_no_sink(self) -> None:
		"""The base configuration of the agent owns the sinks, so a role only adds metrics_* and logs_*."""
		for role in ROLES.glob("*.yaml"):
			self.assertNotIn("sinks:", role.read_text(), role.name)

	def test_the_proxy_also_sends_the_nginx_error_log(self) -> None:
		documents = DatumAgent.get_role_config("proxy").split("\n---\n")

		self.assertEqual(len(documents), 2)
		self.assertIn("logs_nginx", documents[1])

	def test_install_sends_the_script_and_token_over_ssh(self) -> None:
		with (
			patch("atlas.atlas.core.telemetry.agent.get_datum_url", return_value="https://datum.test"),
			patch("atlas.atlas.core.telemetry.agent.issue_datum_token", return_value="datum-jwt"),
			patch.object(SSHRunner, "_run", return_value=SSHResult(output="", exit_code=0)) as run,
		):
			DatumAgent(SSHRunner("192.0.2.1"), "vm-1", "atlas").install(role="proxy")

		source, data, timeout_seconds, _ = run.call_args.args
		self.assertIn("datum.d", source)
		self.assertEqual(data["DATUM_TOKEN"], "datum-jwt")
		self.assertIn("nginx_requests", data["ROLE_CONFIG"])
		self.assertEqual(timeout_seconds, INSTALL_TIMEOUT_SECONDS)
