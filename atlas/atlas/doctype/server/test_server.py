import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import carve_virtual_machine_range
from atlas.atlas.ssh import Connection
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_provider, make_server

# What a boat distribution on the controller looks like: a boat checkout after
# `make build`, or an unpacked release. `Server._boat_uploads` ships exactly these
# four, so a test distribution is these four files with any bytes in them.
BOAT_ARTIFACT_PATHS = ("bin/boat", "sudoers.d/boat", "systemd/boat.service", "systemd/boat-networkd.service")
FAKE_BOAT_BINARY = b"#!/bin/false\nnot really a Go binary\n"
FAKE_BOAT_VERSION = "v0.0.1-test"


def make_boat_distribution(directory: str) -> Path:
	"""Write a complete boat distribution into `directory` and return it."""
	root = Path(directory)
	for relative in BOAT_ARTIFACT_PATHS:
		path = root / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(FAKE_BOAT_BINARY if relative == "bin/boat" else b"# a boat artifact\n")
	return root


def _bootstrap_ssh_side_effect(server):
	"""A `patch` side_effect for `run_ssh` during `Server.bootstrap()` tests.

	`_write_ancp_bootstrap_state`'s read-back canary cats
	`/etc/atlas-networkd/signing-public-key` and asserts the on-disk pub
	equals `Server.signing_public_key`. With a flat `return_value` mock that
	returns the same tuple for every call, the cat returns `"ok"` — which
	never matches the real ed25519 pubkey the controller signed — and the
	canary fires `frappe.throw`, aborting bootstrap long before the assertions
	about install.sh ordering and `run_task` invocation can run. Route the
	cat call to the test's own `Server.signing_public_key` (which the
	controller just wrote via `install -m 0644 /dev/stdin`) and keep
	`("ok", "", 0)` for everything else.

	`_verify_boat_binary` is the second reader with a real answer: it holds the
	host's `sha256sum` against the digest of the binary Atlas shipped and refuses
	`boat version` printing nothing, so a flat `"ok"` fails the install for a
	reason the test never meant to assert."""

	def side_effect(*args, **_kwargs):
		remote_command = args[2] if len(args) > 2 else None
		if remote_command and remote_command.startswith("sudo cat /etc/atlas-networkd/signing-public-key"):
			return ((server.signing_public_key or "") + "\n", "", 0)
		if remote_command and remote_command.startswith("sha256sum "):
			return (f"{hashlib.sha256(FAKE_BOAT_BINARY).hexdigest()}  /usr/local/bin/boat\n", "", 0)
		if remote_command and remote_command.endswith("boat version"):
			return (FAKE_BOAT_VERSION + "\n", "", 0)
		return ("ok", "", 0)

	return side_effect


class TestNetworking(IntegrationTestCase):
	def test_carve_virtual_machine_range(self) -> None:
		self.assertEqual(
			carve_virtual_machine_range("2a03:b0c0:abcd:1234::1", "2a03:b0c0:abcd:1234::/64"),
			"2a03:b0c0:abcd:1234::/124",
		)
		self.assertEqual(
			carve_virtual_machine_range("2400:6180:100:d0:0:1:4ae1:d001", "2400:6180:100:d0::/64"),
			"2400:6180:100:d0:0:1:4ae1:d000/124",
		)


class TestServerBootstrap(IntegrationTestCase):
	def setUp(self) -> None:
		# Every real bootstrap now ships the boat artifacts, so every bootstrap test
		# needs a distribution to ship. A temporary one, not the operator's: the
		# suite must not depend on a boat checkout existing on the machine.
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.boat_distribution = make_boat_distribution(directory.name)
		provider = make_provider("test-provider-server")
		self.server = make_server(
			provider,
			"test-server-bootstrap",
			provider_resource_id="1",
			ipv4_address="10.0.0.5",
			ipv6_address="2a03:b0c0:abcd:1234::1",
			ipv6_prefix="2a03:b0c0:abcd:1234::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:1234::/124",
			status="Bootstrapping",
		)

	def test_bootstrap_installs_boat_then_install_sh_then_runs_the_host_prep_task(self) -> None:
		# The whole bootstrap order, in one assertion each, because every step here
		# is a precondition of the next: boat is installed before install.sh (whose
		# last gate is `command -v boat`) and before the host-prep Task (which IS
		# `boat bootstrap`); boat.service is started only after that Task, because
		# there is nothing for the daemon to adopt until it has run.
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(
			name="task-x",
			stdout='ATLAS_RESULT={"firecracker_version": "", "jailer_version": "", "kernel_version": "", "architecture": ""}',
		)

		# Neutralize the best-effort dashboard ship — it's an independent step with
		# its own test; here we assert the install ordering in isolation.
		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module.Server, "_ship_dashboard"):
				with patch.object(server_module, "upload_files") as upload:
					with patch.object(
						server_module, "run_ssh", side_effect=_bootstrap_ssh_side_effect(self.server)
					) as run_ssh:
						with patch.object(server_module, "run_task", return_value=task) as run:
							with patch.object(
								server_module,
								"connection_for_server",
								return_value=Connection(host="x", ssh_private_key="k"),
							):
								self.server.bootstrap()

		upload.assert_called_once()
		commands = [call.args[2] for call in run_ssh.call_args_list]
		# The allow-list is CHECKED before it is installed. Reversed, a sudoers file
		# sudo cannot parse leaves the boat user with no grants at all.
		self.assertLess(
			commands.index("sudo visudo -cf /var/lib/atlas/boat/sudoers"),
			next(index for index, text in enumerate(commands) if text.startswith("sudo install -m 0440")),
		)
		# The binary lands by rename, and only after the service user its units run
		# as exists.
		self.assertLess(
			next(index for index, text in enumerate(commands) if "useradd" in text),
			next(index for index, text in enumerate(commands) if "mv -f" in text),
		)
		install_sh = next(index for index, text in enumerate(commands) if "install.sh" in text)
		self.assertLess(next(index for index, text in enumerate(commands) if "mv -f" in text), install_sh)
		self.assertLess(commands.index("sudo systemctl daemon-reload"), install_sh)
		run.assert_called_once()
		# The Task is the boat verb, with the Python's two flags.
		self.assertEqual(run.call_args.kwargs["script"], "bootstrap")
		self.assertEqual(
			run.call_args.kwargs["variables"],
			{"FIRECRACKER_VERSION": "v1.16.0", "ARCHITECTURE": "x86_64"},
		)
		# ...and the daemon is started after it, not before.
		self.assertGreater(commands.index("sudo systemctl restart boat.service"), install_sh)
		self.server.reload()
		self.assertEqual(self.server.observed_boat_version, FAKE_BOAT_VERSION)

	def test_bootstrap_aborts_if_install_sh_fails(self) -> None:
		# A non-zero install.sh (broken venv) must fail the bootstrap loudly, before
		# the bootstrap Task ever runs — the carve-out's deep-gate guarantee, moved
		# to install.sh.
		from atlas.atlas.doctype.server import server as server_module

		def only_install_sh_fails(*args, **_kwargs):
			if "install.sh" in args[2]:
				return ("", "boom", 1)
			return _bootstrap_ssh_side_effect(self.server)(*args, **_kwargs)

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module, "upload_files"):
				with patch.object(server_module, "run_ssh", side_effect=only_install_sh_fails):
					with patch.object(server_module, "run_task") as run:
						with patch.object(
							server_module,
							"connection_for_server",
							return_value=Connection(host="x", ssh_private_key="k"),
						):
							with self.assertRaises(frappe.ValidationError) as raised:
								self.server.bootstrap()
		self.assertIn("install.sh failed", str(raised.exception))
		run.assert_not_called()

	def test_bootstrap_aborts_if_the_allow_list_does_not_parse(self) -> None:
		# `visudo -cf` fails ⇒ nothing is installed and the bootstrap stops. This is
		# the failure the order exists for: an unparseable file in /etc/sudoers.d
		# disables the WHOLE directory, so the boat user would lose every grant and
		# every verb on the host would fail at once — and the host would look fine.
		from atlas.atlas.doctype.server import server as server_module

		def visudo_refuses(*args, **_kwargs):
			if args[2].startswith("sudo visudo"):
				return ("", ">>> syntax error near line 3 <<<", 1)
			return _bootstrap_ssh_side_effect(self.server)(*args, **_kwargs)

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module, "upload_files"):
				with patch.object(server_module, "run_ssh", side_effect=visudo_refuses) as run_ssh:
					with patch.object(server_module, "run_task") as run:
						with patch.object(
							server_module,
							"connection_for_server",
							return_value=Connection(host="x", ssh_private_key="k"),
						):
							with self.assertRaises(frappe.ValidationError) as raised:
								self.server.bootstrap()
		self.assertIn("sudoers allow-list", str(raised.exception))
		run.assert_not_called()
		commands = [call.args[2] for call in run_ssh.call_args_list]
		self.assertFalse([text for text in commands if "/etc/sudoers.d/boat" in text])

	def test_bootstrap_aborts_if_the_landed_binary_is_not_the_one_shipped(self) -> None:
		# No signature is checked on this path, so the digest is the whole proof
		# that /usr/local/bin/boat holds the bytes the operator staged. A host
		# reporting anything else stops the bootstrap rather than running verbs
		# through an unknown binary.
		from atlas.atlas.doctype.server import server as server_module

		def wrong_digest(*args, **_kwargs):
			if args[2].startswith("sha256sum "):
				return ("0" * 64 + "  /usr/local/bin/boat\n", "", 0)
			return _bootstrap_ssh_side_effect(self.server)(*args, **_kwargs)

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module, "upload_files"):
				with patch.object(server_module, "run_ssh", side_effect=wrong_digest):
					with patch.object(server_module, "run_task") as run:
						with patch.object(
							server_module,
							"connection_for_server",
							return_value=Connection(host="x", ssh_private_key="k"),
						):
							with self.assertRaises(frappe.ValidationError) as raised:
								self.server.bootstrap()
		self.assertIn("not the binary Atlas shipped", str(raised.exception))
		run.assert_not_called()

	def test_boat_uploads_name_every_missing_artifact(self) -> None:
		# An absent or half-built distribution fails HERE, where nothing has been
		# installed yet, naming the paths and the command that produces them —
		# rather than on the host, halfway through.
		from atlas.atlas.doctype.server import server as server_module

		(self.boat_distribution / "bin" / "boat").unlink()
		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with self.assertRaises(frappe.ValidationError) as raised:
				self.server._boat_uploads()
		self.assertIn("bin/boat", str(raised.exception))
		self.assertIn("make build", str(raised.exception))

	def test_boat_uploads_are_bootstrap_only(self) -> None:
		# The binary ships with a bootstrap and NOT with sync_scripts: refreshing it
		# needs the privileged install and the daemon restart that only bootstrap
		# runs, so a dev-loop scp would leave the binary on disk and the running
		# daemon disagreeing about which one is live.
		from atlas.atlas.doctype.server import server as server_module

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			boat = dict((destination, source) for source, destination in self.server._boat_uploads())
			bootstrap = {destination for _source, destination in self.server._bootstrap_uploads()}
		script_only = {destination for _source, destination in self.server._script_uploads()}

		# Staged, never straight into place: /usr/local/bin/boat is renamed over by
		# _install_boat, and the allow-list is validated before it reaches /etc.
		self.assertIn("/usr/local/bin/boat.incoming", boat)
		self.assertIn("/var/lib/atlas/boat/sudoers", boat)
		self.assertIn("/var/lib/atlas/boat/boat.service", boat)
		self.assertIn("/var/lib/atlas/boat/boat-networkd.service", boat)
		self.assertNotIn("/etc/sudoers.d/boat", boat)
		self.assertTrue(set(boat) <= bootstrap)
		self.assertFalse(set(boat) & script_only)

	def test_script_uploads_ship_task_entry_scripts_durably(self) -> None:
		# The Task entry scripts (provision/start/stop/snapshot-stop) ship to
		# /var/lib/atlas/bin so the runner invokes them in place — no per-Task scp.
		from atlas.atlas import scripts_catalog

		destinations = {dest for _src, dest in self.server._script_uploads()}
		# host_task_scripts() yields VERBS; the FILE (verb→file_for, keeping its
		# .py/.sh suffix on the host disk) is what ships.
		for file_name in ("start-vm.py", "stop-vm.py", "resize-vm.py", "terminate-vm.py"):
			self.assertIn(f"/var/lib/atlas/bin/{file_name}", destinations)
		# The durable set covers every host SSH Task entry point.
		for verb in scripts_catalog.host_task_scripts():
			self.assertIn(f"/var/lib/atlas/bin/{scripts_catalog.file_for(verb)}", destinations)

	def test_bootstrap_ships_the_host_pip_manifest_and_install_sh(self) -> None:
		# install.sh runs `uv pip install /var/lib/atlas/bin`, which needs a
		# pyproject.toml at that root. The host manifest (host-pyproject.toml) must
		# ship there for the install — and install.sh itself must ship so the
		# controller can pipe it over SSH.
		uploads = dict((dest, src) for src, dest in self.server._script_uploads())
		self.assertIn("/var/lib/atlas/bin/pyproject.toml", uploads)
		self.assertTrue(uploads["/var/lib/atlas/bin/pyproject.toml"].endswith("host-pyproject.toml"))
		self.assertIn("/var/lib/atlas/bin/install.sh", uploads)
		self.assertTrue(uploads["/var/lib/atlas/bin/install.sh"].endswith("install.sh"))

	def test_bootstrap_parses_result_line(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		# `boat bootstrap` emits one ATLAS_RESULT=<json> line amid trace noise;
		# the controller parses that, not a bare trailing JSON line.
		stdout = (
			"+ some bash trace\n"
			'ATLAS_RESULT={"firecracker_version": "1.16.0",'
			' "jailer_version": "1.16.0",'
			' "kernel_version": "6.8.0-31-generic",'
			' "architecture": "x86_64"}\n'
		)
		task = fake_task(name="task-y", stdout=stdout)

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module.Server, "_ship_dashboard"):
				with patch.object(server_module, "upload_files"):
					with patch.object(
						server_module, "run_ssh", side_effect=_bootstrap_ssh_side_effect(self.server)
					):
						with patch.object(server_module, "run_task", return_value=task):
							with patch.object(
								server_module,
								"connection_for_server",
								return_value=Connection(host="x", ssh_private_key="k"),
							):
								self.server.bootstrap()
		self.server.reload()
		self.assertEqual(self.server.firecracker_version, "1.16.0")
		self.assertEqual(self.server.jailer_version, "1.16.0")
		self.assertEqual(self.server.kernel_version, "6.8.0-31-generic")
		self.assertEqual(self.server.architecture, "x86_64")
		# A succeeded bootstrap proves the deep sanity gate (atlas --help) passed,
		# so CLI-readiness is persisted once here — no per-Task venv guard.
		self.assertEqual(self.server.cli_ready, 1)

	def test_bootstrap_rejects_from_disallowed_status(self) -> None:
		# `Terminated` is not in BOOTSTRAP_ALLOWED_STATUS. Set in-memory only
		# so the shared server fixture isn't mutated for other tests.
		self.server.status = "Terminated"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.bootstrap()
		self.assertIn("Cannot bootstrap", str(raised.exception))

	def test_get_scripts_returns_operator_visible_scripts(self) -> None:
		from atlas.atlas import scripts_catalog

		entries = self.server.get_scripts()
		# Each entry carries name + intro + fields so the desk Run Task
		# dialog can render itself purely from the response.
		self.assertEqual(
			[entry["name"] for entry in entries],
			scripts_catalog.operator_visible_scripts(),
		)
		for entry in entries:
			self.assertIn("intro", entry)
			self.assertIsInstance(entry["fields"], list)
		# Lifecycle scripts must not leak into the desk picker.
		hidden = {"provision-vm", "start-vm", "stop-vm", "terminate-vm", "restart-vm"}
		self.assertFalse(hidden & {entry["name"] for entry in entries})

	def test_ship_dashboard_uploads_then_enables_socket(self) -> None:
		# When the controller can build the dashboard, _ship_dashboard scp's the
		# manifest and then SSHes the socket-enable command. Best-effort, so we
		# stub the build to a fixed manifest rather than running npm here.
		from atlas.atlas import dashboard
		from atlas.atlas.doctype.server import server as server_module

		manifest = [("/local/server.py", "/opt/atlas-dashboard/server.py")]
		connection = Connection(host="x", ssh_private_key="k")
		with patch.object(dashboard, "dashboard_uploads", return_value=manifest):
			with patch.object(server_module, "upload_files") as upload:
				with patch.object(server_module, "run_ssh", return_value=("", "", 0)) as run_ssh:
					self.server._ship_dashboard(connection)

		upload.assert_called_once_with(connection, manifest)
		# The enable runs the socket unit (socket activation), reachable in the cmd.
		run_ssh.assert_called_once()
		self.assertIn("atlas-dashboard.socket", run_ssh.call_args.args[2])

	def test_ship_dashboard_skips_when_build_unavailable(self) -> None:
		# No build (empty manifest) → ship nothing, enable nothing. A host simply
		# has no dashboard; the bootstrap is unaffected.
		from atlas.atlas import dashboard
		from atlas.atlas.doctype.server import server as server_module

		connection = Connection(host="x", ssh_private_key="k")
		with patch.object(dashboard, "dashboard_uploads", return_value=[]):
			with patch.object(server_module, "upload_files") as upload:
				with patch.object(server_module, "run_ssh") as run_ssh:
					self.server._ship_dashboard(connection)

		upload.assert_not_called()
		run_ssh.assert_not_called()

	def test_ship_dashboard_never_raises_on_error(self) -> None:
		# A dashboard hiccup (here: the build helper itself throwing) must never
		# fail a bootstrap — _ship_dashboard swallows it.
		from atlas.atlas import dashboard
		from atlas.atlas.doctype.server import server as server_module

		connection = Connection(host="x", ssh_private_key="k")
		with patch.object(dashboard, "dashboard_uploads", side_effect=RuntimeError("boom")):
			with patch.object(server_module, "upload_files") as upload:
				# Must return normally despite the raise inside.
				self.server._ship_dashboard(connection)
		upload.assert_not_called()


class TestServerArchive(IntegrationTestCase):
	def setUp(self) -> None:
		# Reset so each test starts from a non-Archived state.
		frappe.db.delete("Server", {"title": "test-server-archive"})
		provider = make_provider("test-provider-archive")
		self.server = make_server(
			provider,
			"test-server-archive",
			provider_resource_id="44",
			ipv4_address="10.0.0.50",
			ipv6_address="2a03:b0c0:abcd:9999::1",
			ipv6_prefix="2a03:b0c0:abcd:9999::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:9999::/124",
			status="Active",
		)

	def test_archive_sets_status_archived(self) -> None:
		from unittest.mock import MagicMock, patch

		with patch("atlas.atlas.atlas_settings.providers.for_provider_type", return_value=MagicMock()):
			self.server.archive()
		self.assertEqual(
			frappe.db.get_value("Server", self.server.name, "status"),
			"Archived",
		)

	def test_archive_throws_when_already_archived(self) -> None:
		from unittest.mock import MagicMock, patch

		with patch("atlas.atlas.atlas_settings.providers.for_provider_type", return_value=MagicMock()):
			self.server.archive()
		self.server.reload()
		with self.assertRaises(frappe.ValidationError):
			self.server.archive()


class TestServerRecover(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Server", {"title": "test-server-recover"})
		self.provider = make_provider("test-provider-recover")

	def _server(
		self, status: str, provider_resource_id: str | None = "99"
	) -> "frappe.model.document.Document":
		return make_server(
			self.provider,
			"test-server-recover",
			provider_resource_id=provider_resource_id,
			status=status,
		)

	def test_recover_re_enqueues_stuck_pending(self) -> None:
		# A Pending row with a vendor id but NULL IPs (the lost-job case) — recover()
		# must re-enqueue finish_provisioning, not run bootstrap directly.
		server = self._server("Pending")
		with patch("atlas.atlas.providers.worker.enqueue_finish_provisioning", return_value=True) as enqueue:
			result = server.recover()
		self.assertTrue(result)
		enqueue.assert_called_once_with(server.name)

	def test_recover_reports_already_in_flight(self) -> None:
		server = self._server("Bootstrapping")
		with patch("atlas.atlas.providers.worker.enqueue_finish_provisioning", return_value=False):
			self.assertFalse(server.recover())

	def test_recover_rejects_active(self) -> None:
		server = self._server("Active")
		with self.assertRaises(frappe.ValidationError):
			server.recover()

	def test_recover_rejects_row_without_resource_id(self) -> None:
		server = self._server("Pending", provider_resource_id=None)
		with self.assertRaises(frappe.ValidationError) as raised:
			server.recover()
		self.assertIn("provider_resource_id", str(raised.exception))


class TestServerSyncImage(IntegrationTestCase):
	def test_sync_image_delegates_to_image_controller(self) -> None:
		from atlas.tests.fixtures import make_image

		provider = make_provider("test-provider-sync")
		server = make_server(
			provider,
			"test-server-sync",
			provider_resource_id="55",
			ipv4_address="10.0.0.55",
			ipv6_address="2a03:b0c0:abcd:8888::1",
			ipv6_prefix="2a03:b0c0:abcd:8888::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:8888::/124",
			status="Active",
		)
		image = make_image("test-image-sync")
		with patch("frappe.enqueue"):
			task_name = server.sync_image(image.name)
		task = frappe.get_doc("Task", task_name)
		self.assertEqual(task.script, "sync-image")
		self.assertEqual(task.server, server.name)


class TestServerImmutability(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Server", {"title": "test-server-immut"})
		provider = make_provider("test-provider-immut")
		self.server = make_server(
			provider,
			"test-server-immut",
			provider_resource_id="66",
			ipv4_address="10.0.0.66",
			ipv6_address="2a03:b0c0:abcd:7777::1",
			ipv6_prefix="2a03:b0c0:abcd:7777::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:7777::/124",
			status="Active",
		)

	def test_provider_type_is_immutable_once_set(self) -> None:
		# The fixture server is provisioned on DigitalOcean; switching the
		# frozen provider_type to a different vendor must be rejected.
		self.server.provider_type = "Scaleway"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.save(ignore_permissions=True)
		self.assertIn("provider_type is immutable", str(raised.exception))

	def test_title_is_immutable_once_set(self) -> None:
		self.server.reload()
		self.server.title = "renamed-server"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.save(ignore_permissions=True)
		self.assertIn("title is immutable", str(raised.exception))

	def test_name_is_a_uuid(self) -> None:
		import uuid

		# Round-trip the UUID parser: raises if `name` isn't a UUID.
		uuid.UUID(self.server.name)

	def test_ipv4_can_be_set_when_initially_blank(self) -> None:
		"""DigitalOcean provision flow: server starts Pending with no IPs;
		`finish_provisioning` later writes them. The immutability check
		should allow None → value transitions."""
		# Reset so the test is hermetic across re-runs (the previous run
		# would have set ipv4_address, which set_only_once then locks).
		frappe.db.delete("Server", {"title": "test-server-blank"})
		blank = make_server(
			make_provider("test-provider-blank"),
			"test-server-blank",
			provider_resource_id="77",
			status="Pending",
		)
		blank.ipv4_address = "10.0.0.77"
		blank.save(ignore_permissions=True)
		blank.reload()
		self.assertEqual(blank.ipv4_address, "10.0.0.77")


class TestServerSigningKeyBackfill(IntegrationTestCase):
	"""M9 — a legacy Server (bootstrapped before the ed25519 signing fields
	existed) must never leave an empty `signing_public_key` in the fleet: the
	backfill patch fills it, and the seed builder never emits an empty entry that
	would force the broken §19.5 relayed-introduction path (spec/31 §19.4/§19.5)."""

	def _make_legacy_server(self, title: str, resource_id: str, hextet: str) -> Document:
		"""An Active Server with its signing keypair blanked out on disk — the
		legacy shape. `validate()` auto-generates the keypair on insert, so we
		clear both fields via `db.set_value` (which bypasses validate) to recreate
		a row that predates the fields."""
		frappe.db.delete("Server", {"title": title})
		server = make_server(
			make_provider(f"test-provider-{title}"),
			title,
			provider_resource_id=resource_id,
			ipv4_address=f"10.0.0.{resource_id}",
			ipv6_address=f"2a03:b0c0:abcd:{hextet}::1",
			ipv6_prefix=f"2a03:b0c0:abcd:{hextet}::/64",
			ipv6_virtual_machine_range=f"2a03:b0c0:abcd:{hextet}::/124",
			status="Active",
		)
		frappe.db.set_value("Server", server.name, "signing_public_key", "", update_modified=False)
		frappe.db.set_value("Server", server.name, "signing_private_key", "", update_modified=False)
		return frappe.get_doc("Server", server.name)

	def test_backfill_patch_populates_every_empty_signing_key(self) -> None:
		from atlas.patches.v1_0.backfill_server_signing_key import execute

		legacy = self._make_legacy_server("test-server-m9-backfill", "81", "8181")
		self.assertFalse(legacy.signing_public_key)

		execute()

		# No Server is left with an empty signing key after the migration.
		self.assertEqual(
			frappe.get_all("Server", filters={"signing_public_key": ("in", ("", None))}, pluck="name"),
			[],
		)
		filled = frappe.get_doc("Server", legacy.name)
		self.assertTrue(filled.signing_public_key)
		# The private half is persisted (encrypted) and reads back decrypted.
		self.assertTrue(filled.get_password("signing_private_key", raise_exception=False))

	def test_seed_build_skips_a_peer_with_an_empty_signing_key(self) -> None:
		from atlas.atlas.doctype.server import server as server_module
		from atlas.atlas.networking import generate_host_signing_keypair

		# The host being bootstrapped — give it a real keypair (persisted, since
		# the seed query and the priv-push read it back from the DB).
		this_host = self._make_legacy_server("test-server-m9-this", "82", "8282")
		priv_b64, pub_b64 = generate_host_signing_keypair()
		this_host.signing_public_key = pub_b64
		this_host.signing_private_key = priv_b64
		this_host.save(ignore_permissions=True)
		this_host = frappe.get_doc("Server", this_host.name)
		# A legacy PEER still missing its key — must NOT appear in the seed.
		legacy_peer = self._make_legacy_server("test-server-m9-peer", "83", "8383")

		captured = {}

		def _capture_seed(*args, **_kwargs):
			# The seed.json write is the one `sudo tee /etc/atlas-networkd/seed.json`.
			remote = args[2] if len(args) > 2 else ""
			stdin = _kwargs.get("stdin", "")
			if "seed.json" in remote and "seed.json.sig" not in remote:
				captured["seed"] = stdin
			# Satisfy the own-key read-back canary (see `_bootstrap_ssh_side_effect`).
			if remote.startswith("sudo cat /etc/atlas-networkd/signing-public-key"):
				return ((this_host.signing_public_key or "") + "\n", "", 0)
			return ("ok", "", 0)

		with patch.object(server_module, "run_ssh", side_effect=_capture_seed):
			this_host._write_ancp_bootstrap_state(Connection(host="x", ssh_private_key="k"))

		import json

		seed = json.loads(captured["seed"])
		host_ids = {entry["host_id"] for entry in seed}
		# The legacy peer with no signing key is skipped entirely...
		self.assertNotIn(legacy_peer.name, host_ids)
		# ...and no emitted entry ever carries an empty signing_public_key.
		self.assertTrue(all(entry["signing_public_key"] for entry in seed))


class TestServerUpgradeBoat(IntegrationTestCase):
	"""`Server.upgrade_boat` — the boat-artifact + unit + durable-script subset of
	bootstrap, made idempotent and re-runnable so a new binary, allow-list line or
	unit reaches an ALREADY-bootstrapped host. `sync_scripts` cannot (it ships
	neither binary nor units), and until this the only path was a full re-bootstrap."""

	def setUp(self) -> None:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.boat_distribution = make_boat_distribution(directory.name)
		provider = make_provider("test-provider-upgrade")
		self.server = make_server(
			provider,
			"test-server-upgrade-boat",
			provider_resource_id="7",
			ipv4_address="10.0.0.7",
			ipv6_address="2a03:b0c0:abcd:5678::1",
			ipv6_prefix="2a03:b0c0:abcd:5678::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:5678::/124",
			status="Active",
		)

	def test_upgrade_reships_and_restarts_without_the_bootstrap_task(self) -> None:
		# It ships the artifacts and installs them in BOOTSTRAP'S order, restarts the
		# daemon onto the new inode and reinstalls the durable code — but never runs
		# the `boat bootstrap` host-prep Task, the one thing that separates
		# re-generating an already-VM-ready host from bootstrapping a fresh one.
		from atlas.atlas.doctype.server import server as server_module

		with patch.object(server_module, "boat_distribution", return_value=self.boat_distribution):
			with patch.object(server_module.Server, "_reinstall_atlas_venv_package") as reinstall:
				with patch.object(server_module, "upload_files") as upload:
					with patch.object(
						server_module, "run_ssh", side_effect=_bootstrap_ssh_side_effect(self.server)
					) as run_ssh:
						with patch.object(server_module, "run_task") as run_task_mock:
							with patch.object(
								server_module,
								"connection_for_server",
								return_value=Connection(host="x", ssh_private_key="k"),
							):
								version = self.server.upgrade_boat()

		upload.assert_called_once()
		reinstall.assert_called_once()
		run_task_mock.assert_not_called()
		commands = [call.args[2] for call in run_ssh.call_args_list]
		# The allow-list is CHECKED before it is installed (a file sudo cannot parse
		# would take out the whole /etc/sudoers.d directory), and the binary lands by
		# rename only after the service user its units run as exists.
		self.assertLess(
			commands.index("sudo visudo -cf /var/lib/atlas/boat/sudoers"),
			next(i for i, t in enumerate(commands) if t.startswith("sudo install -m 0440")),
		)
		self.assertLess(
			next(i for i, t in enumerate(commands) if "useradd" in t),
			next(i for i, t in enumerate(commands) if "mv -f" in t),
		)
		self.assertIn("sudo systemctl daemon-reload", commands)
		self.assertIn("sudo systemctl restart boat.service", commands)
		self.assertIn("sudo systemctl is-active boat.service", commands)
		# The SHA-256 proof of what landed is recorded and returned.
		self.assertEqual(version, FAKE_BOAT_VERSION)
		self.server.reload()
		self.assertEqual(self.server.observed_boat_version, FAKE_BOAT_VERSION)

	def test_upgrade_is_a_noop_on_a_fake_host(self) -> None:
		# A Fake server has no host to reach — like every other host step, upgrade
		# skips its SSH entirely and records nothing.
		from atlas.atlas.doctype.server import server as server_module

		with patch.object(server_module, "is_fake_server", return_value=True):
			with patch.object(server_module, "connection_for_server") as connect:
				with patch.object(server_module, "upload_files") as upload:
					self.assertEqual(self.server.upgrade_boat(), "")
		connect.assert_not_called()
		upload.assert_not_called()

	def test_upgrade_all_hosts_enqueues_one_deduplicated_job_per_active_host(self) -> None:
		# The fleet sweep queues one background upgrade per Active host so a migrate is
		# never blocked on N SSH installs, keyed by host name so a re-run deduplicates.
		from atlas.atlas.doctype.server import server as server_module

		with patch.object(server_module.frappe, "enqueue") as enqueue:
			results = server_module.upgrade_all_hosts_to_current_boat(enqueue=True)

		self.assertEqual(results.get(self.server.name), "queued")
		mine = [call for call in enqueue.call_args_list if call.kwargs.get("server_name") == self.server.name]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0].args[0], "atlas.atlas.doctype.server.server.upgrade_host_to_current_boat")
		self.assertEqual(mine[0].kwargs.get("job_id"), f"upgrade-boat-{self.server.name}")


class TestServerBoatToken(IntegrationTestCase):
	"""WO-1b (spec/33 §12): Atlas mints the per-host bearer token, stores it
	encrypted, re-mints it before its hard expiry, and reads it through
	token_for_server. The install-to-host + daemon reload is the live half."""

	def setUp(self) -> None:
		frappe.db.delete("Server", {"title": "test-server-token"})
		self.provider = make_provider("test-provider-token")

	def _server(self) -> Document:
		return make_server(self.provider, "test-server-token", provider_resource_id="77", status="Active")

	def test_mint_stores_the_token_encrypted_and_stamps_a_future_expiry(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		server = self._server()
		token = server.mint_boat_token()

		self.assertTrue(token)
		# Read back decrypted from __Auth — and NOT sitting in the row column, which
		# is the whole point of a Password field for a bearer secret.
		self.assertEqual(server.get_password("boat_token", raise_exception=False), token)
		self.assertNotEqual(frappe.db.get_value("Server", server.name, "boat_token"), token)
		remaining = frappe.utils.get_datetime(server.boat_token_expires_at) - frappe.utils.now_datetime()
		self.assertGreater(remaining.days, server_module.BOAT_TOKEN_TTL_DAYS - 2)

	def test_current_or_minted_reuses_a_fresh_token(self) -> None:
		server = self._server()
		first = server.mint_boat_token()
		# Still far from expiry, so no new secret is handed out.
		self.assertEqual(server._current_or_minted_boat_token(), first)

	def test_current_or_minted_remints_inside_the_expiry_window(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		server = self._server()
		first = server.mint_boat_token()
		# Push the expiry inside the re-mint window: the next call must mint anew, so a
		# reachable host never carries a token to its hard expiry.
		near = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), days=server_module.BOAT_TOKEN_REMINT_WITHIN_DAYS - 1
		)
		server.db_set("boat_token_expires_at", near)

		second = server._current_or_minted_boat_token()

		self.assertNotEqual(second, first)
		self.assertEqual(server.get_password("boat_token", raise_exception=False), second)

	def test_token_for_server_prefers_the_minted_row_over_config(self) -> None:
		from atlas.atlas.boat_client import token_for_server

		server = self._server()
		token = server.mint_boat_token()
		with patch.dict(frappe.conf, {"atlas_boat_token": "the-config-fallback"}):
			self.assertEqual(token_for_server(server.name), token)

	def test_token_for_server_falls_back_to_config_when_unminted(self) -> None:
		from atlas.atlas.boat_client import token_for_server

		server = self._server()  # never minted
		with patch.dict(frappe.conf, {"atlas_boat_token": "the-config-fallback"}):
			self.assertEqual(token_for_server(server.name), "the-config-fallback")
