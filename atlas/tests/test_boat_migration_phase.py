"""Unit tests for the migration-phase half of the Atlas↔Boat seam (spec/33 §8, item 9).

The cross-host migration saga's host work moves off SSH onto Boat one PHASE at a
time, exactly as the lifecycle verbs did. These prove three things, no daemon
running:

  - the per-phase body mapping — each `migration-*` verb translates to the right
    Boat phase and the right `MigrateRequest` fields, with the UUID-derived
    variables (nbd port/slots, tunnel device/port, route table) dropped because
    Boat re-derives them, and the inject-identity `GuestIdentity` assembled field
    for field;
  - the Hydrating poll takes the GET path, not a journaled POST;
  - `migration._run_phase_task` on a NON-fake host drives `BoatClient.migrate` /
    `get_migration_hydration` and folds the phase's result onto the Task as the one
    `ATLAS_RESULT=` line the controller already parses — so the phase machine cannot
    tell which transport ran it.

The full Fake↔Fake saga stays covered by TestMigrationOverFakeTransport; this file
is the wire-shape proof the Fake path cannot give.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import migration as migration_module
from atlas.atlas import migration_forward
from atlas.atlas.boat_client import (
	MIGRATION_PHASES,
	MIGRATION_POLL_HYDRATION,
	ROUTING_ENVIRONMENT_PATH,
	BoatClient,
	BoatError,
	_migration_phase,
)
from atlas.atlas.task_results import parse_result
from atlas.tests import fixtures
from atlas.tests._mocks import fake_task
from atlas.tests.test_boat_client import REQUEST, _operation, _Response

UUID = "11111111-2222-3333-4444-555555555555"


class TestMigrationPhaseMapping(IntegrationTestCase):
	"""Every `migration-*` verb → (Boat phase, body params). The UUID-derived
	variables must be dropped and the GuestIdentity assembled field for field: a
	wrong mapping silently breaks a migration on a real host."""

	def _build(self, script: str, variables: dict) -> tuple[str, dict]:
		phase, builder = MIGRATION_PHASES[script]
		return phase, builder(variables)

	def test_export_source_sends_only_the_bind_address(self) -> None:
		phase, params = self._build(
			"migration-export-source",
			{"VIRTUAL_MACHINE_NAME": UUID, "NBD_PORT": "10123", "BIND_ADDRESS": "198.51.100.7"},
		)
		self.assertEqual(phase, "export-source")
		# NBD_PORT (UUID-derived) and VIRTUAL_MACHINE_NAME (the path) are dropped.
		self.assertEqual(params, {"bind_address": "198.51.100.7"})

	def test_export_base_sends_image_and_bind_address(self) -> None:
		phase, params = self._build(
			"migration-export-base",
			{"IMAGE_NAME": "bench-16", "NBD_PORT": "10125", "BIND_ADDRESS": "198.51.100.7"},
		)
		self.assertEqual(phase, "export-base")
		self.assertEqual(params, {"image_name": "bench-16", "bind_address": "198.51.100.7"})

	def test_clone_target_sends_sizes_as_ints_and_drops_the_nbd_block(self) -> None:
		phase, params = self._build(
			"migration-clone-target",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"IMAGE_NAME": "bench-16",
				"DISK_GB": "40",
				"DATA_DISK_GB": "0",
				"SOURCE_HOST": "198.51.100.7",
				"NBD_PORT": "10123",
				"NBD_BASE_SLOT": "4",
				"PHASE": "prepare",
			},
		)
		self.assertEqual(phase, "clone-target")
		self.assertEqual(
			params,
			{"image_name": "bench-16", "disk_gb": 40, "data_disk_gb": 0, "source_host": "198.51.100.7"},
		)

	def test_receive_base_carries_the_base_phase(self) -> None:
		for half in ("prepare", "finalize"):
			with self.subTest(half=half):
				phase, params = self._build(
					"migration-receive-base",
					{
						"IMAGE_NAME": "bench-16",
						"DISK_GB": "3",
						"SOURCE_HOST": "198.51.100.7",
						"NBD_PORT": "10125",
						"NBD_BASE_SLOT": "4",
						"PHASE": half,
					},
				)
				self.assertEqual(phase, "receive-base")
				self.assertEqual(
					params,
					{
						"image_name": "bench-16",
						"disk_gb": 3,
						"source_host": "198.51.100.7",
						"base_phase": half,
					},
				)

	def test_inject_identity_assembles_the_guest_identity(self) -> None:
		phase, params = self._build(
			"migration-inject-identity",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"CLONE_DEVICE": f"/dev/mapper/atlas-vm-{UUID}-clone",
				"VIRTUAL_MACHINE_IPV6": "2001:db8:c::2",
				"IPV4_GUEST_CIDR": "10.20.30.2/30",
				"IPV4_GATEWAY": "10.20.30.1",
				"SSH_PUBLIC_KEY": "ssh-ed25519 AAAAkey",
				"DATA_DISK_MOUNT_AT": "/data",
				"ROUTING_BASE_URL": "https://routing.example",
			},
		)
		self.assertEqual(phase, "inject-identity")
		# VIRTUAL_MACHINE_NAME (the path) and CLONE_DEVICE (Boat derives the write
		# device from the UUID) are dropped; ROUTING_BASE_URL becomes one anonymous
		# extra_env file, byte-for-byte the rebuild path's content.
		self.assertEqual(
			params,
			{
				"identity": {
					"ipv6_address": "2001:db8:c::2",
					"ipv4_guest_cidr": "10.20.30.2/30",
					"ipv4_gateway": "10.20.30.1",
					"authorized_keys_blob": "ssh-ed25519 AAAAkey",
					"data_disk_mount_at": "/data",
					"extra_env": [
						{
							"path": ROUTING_ENVIRONMENT_PATH,
							"content": "ATLAS_BASE_URL=https://routing.example\n",
						}
					],
				}
			},
		)

	def test_inject_identity_omits_absent_optional_fields(self) -> None:
		# An empty data mount and no routing URL: neither key is sent (Boat writes a
		# defined value for each either way).
		_phase, params = self._build(
			"migration-inject-identity",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"CLONE_DEVICE": f"/dev/mapper/atlas-vm-{UUID}-clone",
				"VIRTUAL_MACHINE_IPV6": "2001:db8:c::2",
				"IPV4_GUEST_CIDR": "10.20.30.2/30",
				"IPV4_GATEWAY": "10.20.30.1",
				"SSH_PUBLIC_KEY": "ssh-ed25519 AAAAkey",
				"DATA_DISK_MOUNT_AT": "",
				"ROUTING_BASE_URL": "",
			},
		)
		self.assertEqual(
			params["identity"],
			{
				"ipv6_address": "2001:db8:c::2",
				"ipv4_guest_cidr": "10.20.30.2/30",
				"ipv4_gateway": "10.20.30.1",
				"authorized_keys_blob": "ssh-ed25519 AAAAkey",
			},
		)

	def test_collapse_clone_sends_only_the_data_disk_flag(self) -> None:
		phase, params = self._build(
			"migration-cutover-target",
			{"VIRTUAL_MACHINE_NAME": UUID, "DATA_DISK_GB": "0", "NBD_BASE_SLOT": "4"},
		)
		self.assertEqual(phase, "collapse-clone")
		self.assertEqual(params, {"data_disk_gb": 0})

	def test_forward_up_source_carries_only_the_role(self) -> None:
		phase, params = self._build(
			"migration-forward-up",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"ROLE": "source",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"TUNNEL_PORT": "20123",
			},
		)
		self.assertEqual(phase, "forward-up")
		# TUNNEL_DEVICE / TUNNEL_PORT are UUID-derived and dropped; a bare source-role
		# forward-up (no /128 yet) carries only its role.
		self.assertEqual(params, {"role": "source"})

	def test_forward_up_target_adds_the_source_host(self) -> None:
		_phase, params = self._build(
			"migration-forward-up",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"ROLE": "target",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"TUNNEL_PORT": "20123",
				"SOURCE_HOST": "198.51.100.7",
			},
		)
		self.assertEqual(params, {"role": "target", "source_host": "198.51.100.7"})

	def test_forward_up_relay_adds_the_ipv6_and_drops_the_route_table(self) -> None:
		_phase, params = self._build(
			"migration-forward-up",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"ROLE": "source",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"TUNNEL_PORT": "20123",
				"VIRTUAL_MACHINE_IPV6": "2001:db8:b::2",
				"ROUTE_TABLE": "12345",
			},
		)
		self.assertEqual(params, {"role": "source", "virtual_machine_ipv6": "2001:db8:b::2"})

	def test_source_forward_drops_the_reassert_flag(self) -> None:
		phase, params = self._build(
			"migration-source-forward",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"VIRTUAL_MACHINE_IPV6": "2001:db8:b::2",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"REASSERT_PROXY_NDP": "1",
			},
		)
		self.assertEqual(phase, "source-forward")
		# Boat re-asserts proxy-NDP unconditionally, so the flag has no wire field.
		self.assertEqual(params, {"virtual_machine_ipv6": "2001:db8:b::2"})

	def test_target_receive_sends_only_the_ipv6(self) -> None:
		phase, params = self._build(
			"migration-target-receive",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"VIRTUAL_MACHINE_IPV6": "2001:db8:b::2",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"ROUTE_TABLE": "12345",
			},
		)
		self.assertEqual(phase, "target-receive")
		self.assertEqual(params, {"virtual_machine_ipv6": "2001:db8:b::2"})

	def test_forward_down_carries_role_and_ipv6(self) -> None:
		phase, params = self._build(
			"migration-forward-down",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"VIRTUAL_MACHINE_IPV6": "2001:db8:b::2",
				"ROLE": "target",
				"TUNNEL_DEVICE": "atlas-fwd-x",
				"TUNNEL_PORT": "20123",
				"ROUTE_TABLE": "12345",
			},
		)
		self.assertEqual(phase, "forward-down")
		self.assertEqual(params, {"role": "target", "virtual_machine_ipv6": "2001:db8:b::2"})

	def test_withdraw_private_carries_the_128(self) -> None:
		phase, params = self._build(
			"migration-withdraw-private-source",
			{"VIRTUAL_MACHINE_NAME": UUID, "PRIVATE_ADDRESS": "fd00::2"},
		)
		self.assertEqual(phase, "withdraw-private")
		self.assertEqual(params, {"private_address": "fd00::2"})

	def test_withdraw_private_empty_is_a_clean_no_op(self) -> None:
		_phase, params = self._build(
			"migration-withdraw-private-source",
			{"VIRTUAL_MACHINE_NAME": UUID, "PRIVATE_ADDRESS": ""},
		)
		self.assertEqual(params, {"private_address": ""})

	def test_cleanup_source_maps_nbd_pid_and_keep_address(self) -> None:
		phase, params = self._build(
			"migration-cleanup-source",
			{
				"VIRTUAL_MACHINE_NAME": UUID,
				"NBD_PORT": "10123",
				"NBD_PID": "424242",
				"KEEP_ADDRESS": "1",
			},
		)
		self.assertEqual(phase, "cleanup-source")
		# NBD_PORT is UUID-derived and dropped; nbd_pid is reaped and KEEP_ADDRESS="1"
		# maps to Boat's keep_address bool that SUPPRESSES the ingress teardown, so a
		# keep-address migration over Boat no longer black-holes its own forward.
		self.assertEqual(params, {"nbd_pid": 424242, "keep_address": True})

	def test_cleanup_source_keep_address_zero_maps_to_false(self) -> None:
		# The change-address path (KEEP_ADDRESS="0", or absent) tears the ingress down.
		_phase, params = self._build(
			"migration-cleanup-source",
			{"VIRTUAL_MACHINE_NAME": UUID, "NBD_PID": "1", "KEEP_ADDRESS": "0"},
		)
		self.assertEqual(params, {"nbd_pid": 1, "keep_address": False})

	def test_source_autostart_disabled_is_the_pending_default(self) -> None:
		# What Pending sends: ENABLED="0" takes the source unit out of multi-user.target
		# so it stays Stopped across a host reboot (spec/24 §3). The string maps to the
		# `enabled` bool; VIRTUAL_MACHINE_NAME (the path) is dropped.
		phase, params = self._build(
			"migration-source-autostart",
			{"VIRTUAL_MACHINE_NAME": UUID, "ENABLED": "0"},
		)
		self.assertEqual(phase, "source-autostart")
		self.assertEqual(params, {"enabled": False})

	def test_source_autostart_enabled_one_restores_the_unit(self) -> None:
		# The inverse an abandoned migration runs so the resurrected source survives its
		# host's next reboot: ENABLED="1" maps to `enabled` True.
		_phase, params = self._build(
			"migration-source-autostart",
			{"VIRTUAL_MACHINE_NAME": UUID, "ENABLED": "1"},
		)
		self.assertEqual(params, {"enabled": True})

	def test_poll_hydration_is_special_cased_out_of_the_phase_map(self) -> None:
		# The Hydrating poll is a GET with no operation record, not a mutating POST.
		self.assertNotIn(MIGRATION_POLL_HYDRATION, MIGRATION_PHASES)

	def test_an_unmapped_verb_fails_loud(self) -> None:
		with self.assertRaises(BoatError) as raised:
			_migration_phase("provision-vm")
		self.assertIn("provision-vm", str(raised.exception))


class TestBoatClientMigrationWire(IntegrationTestCase):
	"""What Atlas puts on the wire for the two migration endpoints."""

	def setUp(self) -> None:
		self.client = BoatClient(base_url="http://198.51.100.7:8080/v1", token="s3cret")

	def test_migrate_posts_the_phase_path_with_operation_id_and_params(self) -> None:
		with patch(REQUEST, return_value=_Response(payload=_operation(verb="migrate-clone-target"))) as request:
			self.client.migrate(
				"vm-1", "clone-target", operation_id="task-mig-1", params={"image_name": "img", "disk_gb": 40}
			)
		method, url = request.call_args.args
		self.assertEqual(method, "POST")
		self.assertEqual(url, "http://198.51.100.7:8080/v1/vms/vm-1/migrate/clone-target")
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"operation_id": "task-mig-1", "image_name": "img", "disk_gb": 40},
		)

	def test_hydration_get_without_a_clone_device(self) -> None:
		payload = {"hydration_percent": 58, "source_healthy": True}
		with patch(REQUEST, return_value=_Response(payload=payload)) as request:
			result = self.client.get_migration_hydration("vm-1")
		self.assertEqual(
			request.call_args.args, ("GET", "http://198.51.100.7:8080/v1/vms/vm-1/migrate/hydration")
		)
		self.assertEqual(result, payload)

	def test_hydration_get_appends_the_clone_device_query(self) -> None:
		with patch(
			REQUEST, return_value=_Response(payload={"hydration_percent": 100, "source_healthy": True})
		) as request:
			self.client.get_migration_hydration("vm-1", clone_device="atlas-base-bench-16-clone")
		self.assertEqual(
			request.call_args.args[1],
			"http://198.51.100.7:8080/v1/vms/vm-1/migrate/hydration?clone_device=atlas-base-bench-16-clone",
		)


class TestRunMigrationPhaseOverBoat(IntegrationTestCase):
	"""`migration._run_phase_task` on a real (non-Fake) host drives the Boat client
	and lands a Task the phase machine reads exactly as an SSH run's."""

	def setUp(self) -> None:
		provider = fixtures.make_provider("boat-mig-provider")
		self.server = fixtures.make_server(
			provider,
			"boat-mig-server",
			ipv4_address="198.51.100.9",
			ipv6_address="2001:db8:9a::1",
			ipv6_prefix="2001:db8:9a::/64",
			ipv6_virtual_machine_range="2001:db8:9a::/124",
			status="Active",
		)
		self.image = fixtures.make_image("boat-mig-image")
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		self.vm = fixtures.make_virtual_machine(self.server.name, self.image.name)
		self.doc = SimpleNamespace(name="mig-row-1", virtual_machine=self.vm.name)

	def test_mutating_phase_posts_via_migrate_and_folds_the_typed_result(self) -> None:
		operation = {
			"status": "Success",
			"exit_code": 0,
			"output": "clone built\n",
			"result": {"root_clone_device": f"/dev/mapper/atlas-vm-{self.vm.name}-clone"},
		}
		client = MagicMock()
		client.migrate.return_value = operation
		with patch.object(BoatClient, "for_server", return_value=client):
			task = migration_module._run_phase_task(
				self.doc,
				server=self.server.name,
				script="migration-clone-target",
				variables={
					"VIRTUAL_MACHINE_NAME": self.vm.name,
					"IMAGE_NAME": "bench-16",
					"DISK_GB": "40",
					"DATA_DISK_GB": "0",
					"SOURCE_HOST": "198.51.100.7",
					"NBD_PORT": "10123",
					"NBD_BASE_SLOT": "4",
					"PHASE": "prepare",
				},
				timeout_seconds=120,
			)

		# Called with the mapped phase, the Task name as the replay key, and the
		# UUID-derived variables dropped.
		self.assertEqual(client.migrate.call_args.args, (self.vm.name, "clone-target"))
		self.assertEqual(client.migrate.call_args.kwargs["operation_id"], task.name)
		self.assertEqual(
			client.migrate.call_args.kwargs["params"],
			{"image_name": "bench-16", "disk_gb": 40, "data_disk_gb": 0, "source_host": "198.51.100.7"},
		)
		# The Task the phase machine reads: Success, with the operation's typed result
		# folded onto stdout as the ATLAS_RESULT= line parse_result expects.
		self.assertEqual(task.status, "Success")
		self.assertEqual(
			parse_result(task.stdout),
			{"root_clone_device": f"/dev/mapper/atlas-vm-{self.vm.name}-clone"},
		)

	def test_poll_hydration_reads_the_get_and_folds_the_reading(self) -> None:
		client = MagicMock()
		client.get_migration_hydration.return_value = {"hydration_percent": 42, "source_healthy": True}
		with patch.object(BoatClient, "for_server", return_value=client):
			task = migration_module._run_phase_task(
				self.doc,
				server=self.server.name,
				script="migration-poll-hydration",
				variables={"VIRTUAL_MACHINE_NAME": self.vm.name},
				timeout_seconds=60,
			)

		# The poll is the GET, not a claimed POST.
		client.migrate.assert_not_called()
		client.get_migration_hydration.assert_called_once_with(self.vm.name, clone_device=None)
		self.assertEqual(task.status, "Success")
		self.assertEqual(parse_result(task.stdout), {"hydration_percent": 42, "source_healthy": True})

	def test_poll_hydration_passes_the_base_clone_device(self) -> None:
		client = MagicMock()
		client.get_migration_hydration.return_value = {"hydration_percent": 100, "source_healthy": True}
		with patch.object(BoatClient, "for_server", return_value=client):
			migration_module._run_phase_task(
				self.doc,
				server=self.server.name,
				script="migration-poll-hydration",
				variables={"CLONE_DEVICE": "atlas-base-bench-16-clone"},
				timeout_seconds=60,
			)
		client.get_migration_hydration.assert_called_once_with(
			self.vm.name, clone_device="atlas-base-bench-16-clone"
		)


class TestOutOfBandMigrationVerbsDriveBoat(IntegrationTestCase):
	"""The two migration verbs that run OUTSIDE the self-driving phase machine —
	_disable_source_autostart (Pending) and collapse_forward's forward-down
	(operator-initiated) — moved off SSH onto run_boat_migration_phase (item 9).
	These are the transport proof the mapping tests above cannot give: each now
	reaches Boat, and neither reaches run_task."""

	def setUp(self) -> None:
		provider = fixtures.make_provider("boat-oob-provider")
		self.source = fixtures.make_server(
			provider,
			"boat-oob-source",
			ipv4_address="198.51.100.7",
			ipv6_address="2001:db8:7::1",
			ipv6_prefix="2001:db8:7::/64",
			ipv6_virtual_machine_range="2001:db8:7::/124",
			status="Active",
		)
		self.target = fixtures.make_server(
			provider,
			"boat-oob-target",
			ipv4_address="198.51.100.8",
			ipv6_address="2001:db8:8::1",
			ipv6_prefix="2001:db8:8::/64",
			ipv6_virtual_machine_range="2001:db8:8::/124",
			status="Active",
		)
		self.image = fixtures.make_image("boat-oob-image")
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_disable_source_autostart_posts_the_source_autostart_phase(self) -> None:
		vm = fixtures.make_virtual_machine(self.source.name, self.image.name)
		doc = SimpleNamespace(source_server=self.source.name, virtual_machine=vm.name)
		client = MagicMock()
		client.migrate.return_value = {"status": "Success", "exit_code": 0, "output": "ok\n"}
		with (
			patch.object(BoatClient, "for_server", return_value=client),
			# The whole point of the move: it must NOT fall back to run_task.
			patch.object(
				migration_module, "run_task", side_effect=AssertionError("must not touch run_task")
			),
		):
			migration_module._disable_source_autostart(doc)

		# Reached Boat's migrate RPC as the source-autostart phase, disabling the unit
		# (ENABLED="0" -> enabled False); the Task name is the replay key.
		self.assertEqual(client.migrate.call_args.args, (vm.name, "source-autostart"))
		self.assertEqual(client.migrate.call_args.kwargs["params"], {"enabled": False})

	def test_collapse_forward_drives_forward_down_over_boat_not_run_task(self) -> None:
		# A VM as it looks after a keep-address migration: living on the target, still
		# on its source-range /128, forwarded from the source. Stopped so the collapse's
		# converge-the-disk stop is a no-op and this stays a pure transport proof.
		vm = fixtures.make_virtual_machine(self.target.name, self.image.name, status="Stopped")
		vm.flags.migrating = True
		vm.ipv6_address = "2001:db8:7::5"
		vm.traffic_forwarded_from = self.source.name
		vm.traffic_forwarded_since = frappe.utils.now_datetime()
		vm.save(ignore_permissions=True)

		boat_calls: list = []
		task_calls: list = []

		def _boat_spy(*, script, variables, server, virtual_machine, timeout_seconds):
			boat_calls.append((script, variables.get("ROLE"), server))
			return fake_task(stdout="ok")

		def _task_spy(*, script, variables, server, virtual_machine, timeout_seconds):
			task_calls.append(script)
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			# collapse_forward lives in migration_forward now — patch its seams there.
			patch.object(migration_forward, "run_boat_migration_phase", side_effect=_boat_spy),
			patch.object(migration_forward, "run_task", side_effect=_task_spy),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			vm.collapse_forward()

		# Both ends of the forward-down went over Boat's phase transport…
		self.assertIn(("migration-forward-down", "target", self.target.name), boat_calls)
		self.assertIn(("migration-forward-down", "source", self.source.name), boat_calls)
		# …and run_task saw only the cutover re-provision, never forward-down.
		self.assertIn("provision-vm", task_calls)
		self.assertNotIn("migration-forward-down", task_calls)
