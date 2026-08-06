"""Unit tests for Phase 7: Run Task dialog + form extras."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import scripts_catalog
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_provider, make_server


def _server_for(suffix: str) -> "frappe.model.document.Document":
	provider = make_provider("test-provider-server")
	return make_server(
		provider,
		f"test-server-{suffix}",
		provider_resource_id="1",
		ipv4_address="10.0.0.5",
		ipv6_address="2a03:b0c0:abcd:1234::1",
		ipv6_prefix="2a03:b0c0:abcd:1234::/64",
		ipv6_virtual_machine_range="2a03:b0c0:abcd:1234::/124",
		status="Bootstrapping",
	)


class TestRunTaskDialog(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = _server_for("runtask")

	def test_rejects_unknown_script(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self.server.run_task_dialog(script="rm-rf-everything", variables={})

	def test_calls_run_task(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(name="task-runtask-1")

		with patch.object(server_module, "run_task", return_value=task) as run:
			result = self.server.run_task_dialog(
				script="bootstrap-server",
				variables={"FOO": "bar"},
			)

		self.assertEqual(result, "task-runtask-1")
		run.assert_called_once()
		kwargs = run.call_args.kwargs
		self.assertEqual(kwargs["server"], self.server.name)
		self.assertEqual(kwargs["script"], "bootstrap-server")
		self.assertEqual(kwargs["variables"], {"FOO": "bar"})

	def test_parses_string_variables_as_json(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(name="task-runtask-2")

		with patch.object(server_module, "run_task", return_value=task) as run:
			self.server.run_task_dialog(
				script="bootstrap-server",
				variables=json.dumps({"A": "1", "B": "2"}),
			)

		self.assertEqual(run.call_args.kwargs["variables"], {"A": "1", "B": "2"})

	def test_none_variables_becomes_empty_dict(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(name="task-runtask-3")

		with patch.object(server_module, "run_task", return_value=task) as run:
			self.server.run_task_dialog(script="bootstrap-server", variables=None)

		self.assertEqual(run.call_args.kwargs["variables"], {})

	def test_rejects_non_dict_after_json_parse(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.run_task_dialog(
				script="bootstrap-server",
				variables=json.dumps([1, 2, 3]),
			)
		self.assertIn("JSON object", str(raised.exception))

	def test_reboot_invokes_reboot_script(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(name="task-reboot-1")

		with patch.object(server_module, "run_task", return_value=task) as run:
			result = self.server.reboot()

		self.assertEqual(result, "task-reboot-1")
		self.assertEqual(run.call_args.kwargs["script"], "reboot-server")
		self.assertEqual(run.call_args.kwargs["variables"], {})


class TestScriptsCatalog(IntegrationTestCase):
	def test_allowed_scripts_lists_real_verbs(self) -> None:
		scripts = scripts_catalog.allowed_scripts()
		# allowed_scripts() now speaks VERBS (suffix-less); the on-disk files keep
		# their .py/.sh extension.
		self.assertIn("bootstrap-server", scripts)
		self.assertIn("reboot-server", scripts)
		self.assertIn("provision-vm", scripts)

	def test_allowed_scripts_are_suffixless_verbs(self) -> None:
		scripts = scripts_catalog.allowed_scripts()
		# nothing under scripts/guest/, scripts/systemd/ or scripts/lib/ leaks in
		# (lib/ holds the importable atlas package, not Task entry points), and the
		# returned values are verbs, never filenames.
		for entry in scripts:
			self.assertFalse(entry.endswith((".py", ".sh")), entry)
			self.assertNotIn("/", entry)
		# the systemd hooks live in scripts/ but are excluded as non-Tasks
		for hook in scripts_catalog.SYSTEMD_HOOKS:
			self.assertNotIn(hook, scripts)
