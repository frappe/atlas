from frappe.tests import UnitTestCase

from atlas.atlas.core.telemetry.agent import ROLES
from atlas.atlas.core.telemetry.collectors import SERVICE_DOCTYPES


class TestDatumAgentRoles(UnitTestCase):
	def test_each_service_has_a_role_file(self) -> None:
		for service in SERVICE_DOCTYPES.values():
			self.assertTrue((ROLES / f"{service}.yaml").is_file(), service)

	def test_a_role_file_adds_no_sink(self) -> None:
		"""The base configuration of the agent owns the sinks, so a role only adds metrics_* and logs_*."""
		for role in ROLES.glob("*.yaml"):
			self.assertNotIn("sinks:", role.read_text(), role.name)
