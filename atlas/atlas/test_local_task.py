"""Unit tests for the controller-local Task runner — the non-SSH sibling of the
SSH runner. The subprocess itself is mocked (no script actually runs); what's
tested is that a Task row is recorded, the argv carries the kebab flags, secrets
go through env not argv, and a non-zero exit surfaces as a Failure + raise."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import local_task


class TestRunLocalTask(IntegrationTestCase):
	def _fake_completed(self, stdout="", stderr="", returncode=0):
		return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

	def test_records_task_and_returns_typed_stdout(self) -> None:
		out = 'ATLAS_RESULT={"ok": 1}\nIssued.'
		with (
			patch.object(local_task.scripts_catalog, "resolve", return_value="/repo/scripts/issue-cert.py"),
			patch.object(local_task.subprocess, "run", return_value=self._fake_completed(stdout=out)) as run,
		):
			task = local_task.run_local_task(
				script="issue-cert",
				variables={"DOMAIN": "blr1.frappe.dev", "DNS_AUTHENTICATOR": "route53"},
				env={"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh"},
			)

		self.assertEqual(task.status, "Success")
		self.assertIn("ATLAS_RESULT", task.stdout)
		self.assertTrue(frappe.db.exists("Task", task.name))

		argv = run.call_args.args[0]
		# kebab flags rendered from the variables dict
		self.assertIn("--domain", argv)
		self.assertIn("blr1.frappe.dev", argv)
		self.assertIn("--dns-authenticator", argv)
		# secrets travel via env, never argv
		self.assertFalse(any("AKIA" in str(token) for token in argv))
		self.assertEqual(run.call_args.kwargs["env"]["AWS_ACCESS_KEY_ID"], "AKIA")

	def test_nonzero_exit_marks_failure_and_raises(self) -> None:
		with (
			patch.object(local_task.scripts_catalog, "resolve", return_value="/repo/scripts/issue-cert.py"),
			patch.object(
				local_task.subprocess,
				"run",
				return_value=self._fake_completed(stderr="certbot blew up", returncode=1),
			),
		):
			with self.assertRaises(frappe.ValidationError):
				local_task.run_local_task(script="issue-cert", variables={"DOMAIN": "x"})

		# The Task row was still recorded, as Failure.
		last = frappe.get_all(
			"Task",
			filters={"script": "issue-cert"},
			fields=["status", "stderr"],
			order_by="creation desc",
			limit=1,
		)
		self.assertEqual(last[0].status, "Failure")
		self.assertIn("certbot blew up", last[0].stderr)
