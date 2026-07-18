"""Unit tests for the dashboard ship helpers (`atlas.atlas.dashboard`).

The dashboard is best-effort: it must build on the controller and ship onto a
host, but never fail a bootstrap and never ship stale assets. These cover the
build guards, the freshness/no-stale property, and the upload manifest shape —
all with the build subprocess mocked, so no npm is needed to run the suite.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from atlas.atlas import dashboard


class _CompletedProcess:
	def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
		self.returncode = returncode
		self.stderr = stderr
		self.stdout = stdout


class TestDashboardBuild(FrappeTestCase):
	def test_build_skips_when_npm_absent(self) -> None:
		# No npm on PATH → None, silently. The caller ships nothing.
		with patch.object(dashboard.shutil, "which", return_value=None):
			self.assertIsNone(dashboard.build_dashboard())
			self.assertEqual(dashboard.dashboard_uploads(), [])

	def test_build_failure_returns_none_and_ships_nothing(self) -> None:
		# A non-zero npm build → None; no assets shipped even if a stale dist/
		# exists (build_dashboard wipes dist before building, so nothing lingers).
		with patch.object(dashboard.shutil, "which", return_value="/usr/bin/npm"):
			with patch.object(dashboard.Path, "exists", return_value=True):
				with patch.object(dashboard.shutil, "rmtree"):
					with patch.object(
						dashboard.subprocess, "run", return_value=_CompletedProcess(1, stderr="boom")
					):
						self.assertIsNone(dashboard.build_dashboard())

	def test_uploads_empty_when_build_unavailable(self) -> None:
		with patch.object(dashboard, "build_dashboard", return_value=None):
			self.assertEqual(dashboard.dashboard_uploads(), [])

	def test_uploads_manifest_shape(self) -> None:
		# Given a (fake) fresh dist, the manifest ships server.py, both units, and
		# every dist file under REMOTE_ROOT/dist — the self-consistent bundle.
		import tempfile
		from pathlib import Path

		with tempfile.TemporaryDirectory() as tmp:
			dist = Path(tmp) / "dist"
			(dist / "assets").mkdir(parents=True)
			(dist / "index.html").write_text("<!doctype html>")
			(dist / "assets" / "app.js").write_text("//")
			with patch.object(dashboard, "build_dashboard", return_value=dist):
				uploads = dashboard.dashboard_uploads()

		remotes = {remote for _local, remote in uploads}
		self.assertIn("/opt/atlas-dashboard/server.py", remotes)
		self.assertIn("/etc/systemd/system/atlas-dashboard.socket", remotes)
		self.assertIn("/etc/systemd/system/atlas-dashboard.service", remotes)
		self.assertIn("/opt/atlas-dashboard/dist/index.html", remotes)
		self.assertIn("/opt/atlas-dashboard/dist/assets/app.js", remotes)

	def test_enable_command_enables_socket_not_service(self) -> None:
		# Socket activation: enable the .socket (which starts the service on
		# demand), after a daemon-reload for the new unit files.
		command = dashboard.enable_command()
		self.assertIn("daemon-reload", command)
		self.assertIn("enable --now atlas-dashboard.socket", command)
		self.assertNotIn("enable --now atlas-dashboard.service", command)
