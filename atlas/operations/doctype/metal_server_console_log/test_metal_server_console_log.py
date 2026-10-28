# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.core.ssh import SSHResult

MODULE = "atlas.operations.doctype.metal_server_console_log.metal_server_console_log"


def insert_server(title: str, public_ipv4_address: str, status: str = "Running") -> str:
	name = str(uuid4())
	frappe.get_doc(
		{
			"doctype": "Metal Server",
			"name": name,
			"title": title,
			"status": status,
			"public_ipv4_address": public_ipv4_address,
			"server_size": "test-size",
			"server_image": "test-image",
		}
	).db_insert()
	return name


def insert_log(servers: str) -> frappe.Document:
	return frappe.get_doc(
		{
			"doctype": "Metal Server Console Log",
			"servers": servers,
			"script": "uptime",
			"timeout_seconds": 300,
		}
	).insert(ignore_permissions=True)


class FakeSSHRunner:
	def __init__(self, host: str) -> None:
		self.host = host

	def run_command(self, script, *, timeout_seconds, on_output):
		if self.host == "192.0.2.12":
			raise OSError("Connection refused")
		on_output(f"{script} on {self.host}")
		return SSHResult(f"{script} on {self.host}", 0 if self.host == "192.0.2.10" else 3)


class TestServerConsoleLog(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.db.rollback)
		self.first = insert_server("metal-console-test-1", "192.0.2.10")
		self.second = insert_server("metal-console-test-2", "192.0.2.11")
		self.third = insert_server("metal-console-test-3", "192.0.2.12")

	def test_servers_match_by_title_name_and_ip_address_once(self) -> None:
		log = insert_log(f"metal-console-test-1, {self.second}\n192.0.2.12,192.0.2.10")

		self.assertEqual([row.server for row in log.results], [self.first, self.second, self.third])
		self.assertEqual([row.ssh_host for row in log.results], ["192.0.2.10", "192.0.2.11", "192.0.2.12"])

	def test_unknown_or_deleted_server_stops_the_run(self) -> None:
		insert_server("metal-console-test-4", "192.0.2.13", status="Deleted")

		for entry in ("metal-console-missing", "metal-console-test-4"):
			with self.subTest(entry=entry), self.assertRaises(frappe.ValidationError):
				insert_log(f"metal-console-test-1,{entry}")

	def test_one_failed_server_does_not_stop_the_others(self) -> None:
		log = insert_log("metal-console-test-1,metal-console-test-2,metal-console-test-3")

		with (
			patch(f"{MODULE}.SSHRunner", FakeSSHRunner),
			patch.object(frappe.db, "commit"),
			patch(f"{MODULE}.frappe.publish_realtime") as publish_realtime,
		):
			log.execute()

		log.reload()
		self.assertEqual(log.status, "Failed")
		self.assertEqual([row.status for row in log.results], ["Success", "Failed", "Failed"])
		self.assertEqual([row.exit_code for row in log.results[:2]], [0, 3])
		self.assertEqual(log.results[0].output, "uptime on 192.0.2.10")
		self.assertIn("Connection refused", log.results[2].output)

		published_output = {row.name: "" for row in log.results}
		for call in publish_realtime.call_args_list:
			for change in call.args[1]["results"]:
				self.assertEqual(change["offset"], len(published_output[change["name"]]))
				published_output[change["name"]] += change["new_output"]
		self.assertEqual(published_output, {row.name: row.output for row in log.results})

	def test_scrubbed_run_stores_and_streams_masked_output(self) -> None:
		log = insert_log("metal-console-test-1")
		log.db_set("scrub_output", 1)

		class SecretSSHRunner:
			def __init__(self, host: str) -> None:
				pass

			def run_command(self, script, *, timeout_seconds, on_output):
				for chunk in ("ok\npass", "word=hunter2\n", "token=abc"):
					on_output(chunk)
				return SSHResult("", 0)

		with (
			patch(f"{MODULE}.SSHRunner", SecretSSHRunner),
			patch.object(frappe.db, "commit"),
			patch(f"{MODULE}.frappe.publish_realtime") as publish_realtime,
		):
			log.execute()

		log.reload()
		self.assertEqual(log.results[0].output, "ok\npassword=[REDACTED]\ntoken=[REDACTED]")
		published = "".join(
			change["new_output"]
			for call in publish_realtime.call_args_list
			for change in call.args[1]["results"]
		)
		self.assertEqual(published, log.results[0].output)
