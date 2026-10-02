from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.service.core.warpgate.client import WarpgateError
from atlas.service.core.warpgate.sync import WarpgateTargetSync


class FakeWarpgate:
	"""Keep targets, roles, and target roles in memory, like the Warpgate admin API."""

	def __init__(self) -> None:
		self.targets: dict[str, dict] = {}
		self.roles: dict[str, str] = {}
		self.target_roles: dict[str, set[str]] = {}
		self.writes: list[str] = []
		self.refused: set[str] = set()
		self.known_hosts: dict[str, tuple[str, str]] = {}

	def list_known_hosts(self) -> list[dict]:
		return [{"host": host} for host in self.known_hosts]

	def add_known_host(self, host: str, key_type: str, key_base64: str) -> None:
		self.known_hosts[host] = (key_type, key_base64)

	def get_public_keys(self) -> list[str]:
		return ["ssh-ed25519 AAAAwarpgate"]

	def list_targets(self) -> list[dict]:
		return list(self.targets.values())

	def create_target(self, data: dict) -> dict:
		if data["name"] in self.refused:
			raise WarpgateError("Name already exists")
		target = {**data, "id": f"target-{data['description'].rsplit(' ', 1)[-1]}"}
		self.targets[target["id"]] = target
		self.target_roles[target["id"]] = set()
		self.writes.append(f"create {data['name']}")
		return target

	def update_target(self, target_id: str, data: dict) -> dict:
		self.targets[target_id] = {**data, "id": target_id}
		self.writes.append(f"update {data['name']}")
		return self.targets[target_id]

	def delete_target(self, target_id: str) -> None:
		self.writes.append(f"delete {self.targets.pop(target_id)['name']}")

	def list_target_roles(self, target_id: str) -> list[dict]:
		return [{"id": role_id} for role_id in self.target_roles[target_id]]

	def add_target_role(self, target_id: str, role_id: str) -> None:
		self.target_roles[target_id].add(role_id)

	def list_roles(self) -> list[dict]:
		return [{"name": name, "id": role_id} for name, role_id in self.roles.items()]

	def create_role(self, name: str) -> dict:
		self.roles[name] = f"role-{name}"
		return {"id": self.roles[name]}

	def rename_role(self, role_id: str, name: str) -> None:
		old = next(old for old, value in self.roles.items() if value == role_id)
		self.roles[name] = self.roles.pop(old)
		self.writes.append(f"rename role {old} to {name}")

	def delete_role(self, role_id: str) -> None:
		name = next(name for name, value in self.roles.items() if value == role_id)
		del self.roles[name]
		self.writes.append(f"delete role {name}")

	def role_names(self, server: str) -> set[str]:
		names = {value: name for name, value in self.roles.items()}
		return {names[role_id] for role_id in self.target_roles[f"target-{server}"]}


def report_errors(failures: list[str]) -> None:
	if failures:
		raise WarpgateError("; ".join(failures))


class TestWarpgateTargetSync(UnitTestCase):
	def run_sync(
		self, warpgate: FakeWarpgate, hosts: dict[str, tuple[str, str]], status: str = "Stopped"
	) -> None:
		"""Run once. Stopped hosts skip the SSH trust step unless a test asks for Running."""
		with (
			patch.object(
				WarpgateTargetSync,
				"get_hosts",
				return_value={name: (*host, status) for name, host in hosts.items()},
			),
			patch.object(WarpgateTargetSync, "report_failures", side_effect=report_errors),
		):
			WarpgateTargetSync(warpgate).run()

	def test_each_host_gets_a_target_with_its_role_and_all_hosts(self) -> None:
		warpgate = FakeWarpgate()

		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1"), "server-2": ("osa-2", "fdab:3::2")})

		self.assertEqual(warpgate.role_names("server-1"), {"all-hosts", "host:osa-1"})
		self.assertEqual(warpgate.role_names("server-2"), {"all-hosts", "host:osa-2"})
		self.assertEqual(warpgate.targets["target-server-1"]["options"]["host"], "fdab:3::1")

	def test_a_second_run_writes_nothing(self) -> None:
		warpgate = FakeWarpgate()
		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")})
		warpgate.writes.clear()

		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")})

		self.assertEqual(warpgate.writes, [])

	def test_a_new_address_updates_the_target(self) -> None:
		warpgate = FakeWarpgate()
		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")})

		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::9")})

		self.assertEqual(warpgate.targets["target-server-1"]["options"]["host"], "fdab:3::9")

	def test_a_removed_host_loses_its_target_and_role(self) -> None:
		warpgate = FakeWarpgate()
		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1"), "server-2": ("osa-2", "fdab:3::2")})

		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")})

		self.assertEqual([target["name"] for target in warpgate.list_targets()], ["osa-1"])
		self.assertNotIn("host:osa-2", warpgate.roles)
		self.assertIn("all-hosts", warpgate.roles)

	def test_a_target_that_atlas_does_not_manage_is_left_alone(self) -> None:
		warpgate = FakeWarpgate()
		warpgate.targets["manual"] = {
			**WarpgateTargetSync.get_target_data("server-9", "bastion", "fdab:3::7"),
			"id": "manual",
			"description": "",
		}

		self.run_sync(warpgate, {})

		self.assertIn("manual", warpgate.targets)

	def test_one_failing_host_does_not_stop_the_others(self) -> None:
		warpgate = FakeWarpgate()
		warpgate.refused.add("osa-1")

		with self.assertRaisesRegex(WarpgateError, "osa-1"):
			self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1"), "server-2": ("osa-2", "fdab:3::2")})

		self.assertEqual(warpgate.targets["target-server-2"]["name"], "osa-2")

	def test_a_renamed_host_keeps_its_target_role_and_grants(self) -> None:
		"""Grants point at the role ID, so the role is renamed, not replaced."""
		warpgate = FakeWarpgate()
		self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")})
		role_id = warpgate.roles["host:osa-1"]

		self.run_sync(warpgate, {"server-1": ("par-1", "fdab:3::1")})

		self.assertEqual(warpgate.targets["target-server-1"]["name"], "par-1")
		self.assertEqual(warpgate.roles["host:par-1"], role_id)
		self.assertNotIn("host:osa-1", warpgate.roles)
		self.assertEqual(warpgate.role_names("server-1"), {"all-hosts", "host:par-1"})

	def test_a_running_host_trusts_warpgate_once_and_pins_its_host_key(self) -> None:
		warpgate = FakeWarpgate()
		result = SimpleNamespace(is_success=True, output="ssh-ed25519 AAAAhost root@osa-1\n")

		with patch("atlas.service.core.warpgate.sync.SSHRunner") as ssh_runner:
			ssh_runner.return_value.run_command.return_value = result
			self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")}, status="Running")
			self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")}, status="Running")

		ssh_runner.assert_called_once_with("fdab:3::1")
		data = ssh_runner.return_value.run_command.call_args.kwargs["data"]
		self.assertEqual(data, {"WARPGATE_KEYS": "ssh-ed25519 AAAAwarpgate"})
		self.assertEqual(warpgate.known_hosts["fdab:3::1"], ("ssh-ed25519", "AAAAhost"))

	def test_a_stopped_host_is_not_contacted(self) -> None:
		warpgate = FakeWarpgate()

		with patch("atlas.service.core.warpgate.sync.SSHRunner") as ssh_runner:
			self.run_sync(warpgate, {"server-1": ("osa-1", "fdab:3::1")}, status="Stopped")

		ssh_runner.assert_not_called()
		self.assertIn("target-server-1", warpgate.targets)

	def test_an_unreachable_host_does_not_stop_the_others(self) -> None:
		warpgate = FakeWarpgate()
		hosts = {"server-1": ("osa-1", "fdab:3::1"), "server-2": ("osa-2", "fdab:3::2")}
		reached = SimpleNamespace(is_success=True, output="ssh-ed25519 AAAAhost root@osa-2\n")

		with patch("atlas.service.core.warpgate.sync.SSHRunner") as ssh_runner:
			ssh_runner.return_value.run_command.side_effect = [OSError("no route to host"), reached]
			with self.assertRaisesRegex(WarpgateError, "osa-1.*no route to host"):
				self.run_sync(warpgate, hosts, status="Running")

		self.assertIn("fdab:3::2", warpgate.known_hosts)


class TestReportFailures(UnitTestCase):
	def test_the_same_failure_is_reported_once_and_success_clears_it(self) -> None:
		cache = {}
		fake_cache = SimpleNamespace(
			get_value=cache.get,
			set_value=lambda key, value, expires_in_sec: cache.update({key: value}),
			delete_value=lambda key: cache.pop(key, None),
		)
		with patch("atlas.service.core.warpgate.sync.frappe.cache", fake_cache):
			with self.assertRaises(WarpgateError):
				WarpgateTargetSync.report_failures(["osa-1: refused"])
			WarpgateTargetSync.report_failures(["osa-1: refused"])
			with self.assertRaises(WarpgateError):
				WarpgateTargetSync.report_failures(["osa-2: refused"])
			WarpgateTargetSync.report_failures([])
			with self.assertRaises(WarpgateError):
				WarpgateTargetSync.report_failures(["osa-2: refused"])
