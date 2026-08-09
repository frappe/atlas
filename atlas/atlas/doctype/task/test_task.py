import json

import frappe
from frappe.tests import IntegrationTestCase


class TestTask(IntegrationTestCase):
	def _make(self, **overrides) -> "frappe.model.document.Document":
		defaults = {
			"doctype": "Task",
			"server": None,
			"script": "noop",
			"variables": json.dumps({"FOO": "bar"}),
			"status": "Pending",
			"triggered_by": "Administrator",
		}
		defaults.update(overrides)
		return frappe.get_doc(defaults).insert(ignore_permissions=True)

	def test_task_insert_defaults(self) -> None:
		task = self._make(server=None, script="echo")
		self.assertEqual(task.status, "Pending")
		self.assertEqual(task.script, "echo")
		self.assertIsNone(task.exit_code)

	def test_task_variables_must_be_json(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Task",
					"script": "noop",
					"variables": "{not json",
					"status": "Pending",
					"triggered_by": "Administrator",
				}
			).insert(ignore_permissions=True)

	def test_task_immutable_after_insert(self) -> None:
		task = self._make()
		task.script = "different"
		with self.assertRaises(frappe.ValidationError):
			task.save(ignore_permissions=True)

	def test_variables_dict_property_round_trips(self) -> None:
		task = self._make(variables=json.dumps({"FOO": "bar", "BAZ": "qux"}))
		self.assertEqual(task.variables_dict, {"FOO": "bar", "BAZ": "qux"})

	def test_variables_dict_property_returns_empty_when_variables_empty(self) -> None:
		# Construct in-memory (don't insert; insert validates non-empty).
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"script": "noop",
				"variables": "",
				"status": "Pending",
				"triggered_by": "Administrator",
			}
		)
		self.assertEqual(task.variables_dict, {})

	def test_variables_dict_setter_serializes_to_json(self) -> None:
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"script": "noop",
				"variables": "{}",
				"status": "Pending",
				"triggered_by": "Administrator",
			}
		)
		task.variables_dict = {"NAME": "alice"}
		self.assertEqual(json.loads(task.variables), {"NAME": "alice"})

	def test_variables_dict_setter_rejects_non_dict(self) -> None:
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"script": "noop",
				"variables": "{}",
				"status": "Pending",
				"triggered_by": "Administrator",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			task.variables_dict = "not a dict"

	def test_validate_rejects_empty_variables(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.get_doc(
				{
					"doctype": "Task",
					"script": "noop",
					"variables": "",
					"status": "Pending",
					"triggered_by": "Administrator",
				}
			).insert(ignore_permissions=True)
		self.assertIn("variables", str(raised.exception))

	def test_validate_immutability_skips_when_no_before_save(self) -> None:
		# Defensive branch: a non-new doc whose `_doc_before_save` was cleared
		# should pass immutability validation without comparing fields.
		task = self._make()
		task.script = "different"
		task._doc_before_save = None
		# `_validate_immutability` should early-return rather than throw.
		task._validate_immutability()

	def test_validate_rejects_non_object_json(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.get_doc(
				{
					"doctype": "Task",
					"script": "noop",
					"variables": "[1, 2, 3]",
					"status": "Pending",
					"triggered_by": "Administrator",
				}
			).insert(ignore_permissions=True)
		self.assertIn("JSON object", str(raised.exception))

	def test_subject_set_for_known_script(self) -> None:
		from atlas.tests.fixtures import make_server

		server = make_server(title="task-test-server-subject")
		task = self._make(script="bootstrap-server", server=server.name)
		# Subject is just the verb-noun label now — target identity lives on
		# the Server column, not in the subject.
		self.assertEqual(task.subject, "Bootstrap Server")

	def test_subject_set_for_unknown_script_falls_back_to_verb(self) -> None:
		from atlas.tests.fixtures import make_server

		server = make_server(title="task-test-server-subject-unknown")
		task = self._make(script="noop", server=server.name)
		# A verb with no SCRIPT_LABELS entry falls back to the raw verb string.
		self.assertEqual(task.subject, "noop")

	def test_subject_without_server_or_vm(self) -> None:
		task = self._make(script="bootstrap-server", server=None)
		self.assertEqual(task.subject, "Bootstrap Server")

	def test_states_array_paints_status_pills(self) -> None:
		"""DocType `states` array drives the list-view colour pill for the
		Status column. Pinning the colour mapping here so a future PR can't
		silently re-colour Failure (or drop a status)."""
		import json
		import pathlib

		json_path = pathlib.Path(__file__).parent / "task.json"
		schema = json.loads(json_path.read_text())
		states = {row["title"]: row["color"] for row in schema["states"]}
		self.assertEqual(
			states,
			{"Pending": "Yellow", "Running": "Blue", "Success": "Green", "Failure": "Red"},
		)

	def test_retry_rejects_non_failure(self) -> None:
		task = self._make(script="bootstrap-server", server=None, status="Pending")
		with self.assertRaises(frappe.ValidationError) as raised:
			task.retry()
		self.assertIn("failed", str(raised.exception).lower())

	def test_retry_rejects_non_retriable_script(self) -> None:
		task = self._make(script="noop", server=None)
		# Drive into Failure without going through the runner.
		frappe.db.set_value("Task", task.name, "status", "Failure")
		task.reload()
		with self.assertRaises(frappe.ValidationError) as raised:
			task.retry()
		self.assertIn("not retriable", str(raised.exception).lower())
