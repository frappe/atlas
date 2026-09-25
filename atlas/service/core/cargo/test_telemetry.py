# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

import atlas
import atlas.service.core.cargo.telemetry as telemetry
from atlas.service.core.cargo.telemetry import (
	remove_telemetry_config,
	store_telemetry_config,
	telemetry_config_json,
)

VALID_CONFIG = {"telemetry": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 20}}


class TestTelemetryConfig(UnitTestCase):
	def setUp(self) -> None:
		site_directory = TemporaryDirectory()
		self.addCleanup(site_directory.cleanup)
		patcher = patch.object(
			telemetry.frappe,
			"get_site_path",
			side_effect=lambda *parts: str(Path(site_directory.name).joinpath(*parts)),
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_stored_datum_host_is_read_back_as_compact_json(self) -> None:
		store_telemetry_config(VALID_CONFIG)

		self.assertEqual(json.loads(telemetry_config_json())["telemetry"], VALID_CONFIG["telemetry"])
		self.assertNotIn(" ", telemetry_config_json())

	def test_installation_fails_loudly_without_a_stored_datum_host(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "No telemetry configuration"):
			telemetry_config_json()

	def test_an_archived_datum_host_is_forgotten(self) -> None:
		store_telemetry_config(VALID_CONFIG)
		remove_telemetry_config()
		remove_telemetry_config()

		self.assertFalse(telemetry.config_file().exists())

	def test_the_datum_host_size_is_validated(self) -> None:
		with self.assertRaisesRegex(
			frappe.ValidationError, "telemetry must hold cpu_millicores, ram_gb and disk_gb"
		):
			store_telemetry_config(VALID_CONFIG | {"telemetry": {"cpu_millicores": 2000}})

	def test_the_installer_writes_the_stored_datum_host_to_site_config(self) -> None:
		script = (Path(atlas.__file__).parent / "scripts" / "install-cargo.sh").read_text()

		self.assertRegex(
			script,
			re.compile(r"set-config -p default_telemetry_config \$q_telemetry_config"),
		)
		self.assertIn(
			"for name in $ENROLMENT_VARS DEFAULT_STORAGE_CLUSTER_CONFIG DEFAULT_TELEMETRY_CONFIG; do", script
		)
