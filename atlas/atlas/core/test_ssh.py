from __future__ import annotations

import subprocess
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.ssh import SSHResult, SSHRunner, wait_for_server


class TestSSHRunner(UnitTestCase):
	def test_run_command_passes_host_port_and_data(self) -> None:
		runner = SSHRunner("203.0.113.1", 2222, "ubuntu")
		process = Mock()
		process.stdin = Mock()
		process.wait.return_value = 0

		with (
			patch("atlas.atlas.core.ssh.subprocess.Popen", return_value=process) as popen,
			patch.object(runner, "_read_output", return_value="done"),
		):
			outcome = runner.run_command("echo $MESSAGE", data={"MESSAGE": "hello world"})

		self.assertEqual(outcome.output, "done")
		self.assertEqual(outcome.exit_code, 0)
		self.assertTrue(outcome.is_success)
		self.assertIn("-p", popen.call_args.args[0])
		self.assertIn("ubuntu@203.0.113.1", popen.call_args.args[0])
		self.assertNotIn("input", popen.call_args.kwargs)
		self.assertEqual(
			process.stdin.write.call_args.args[0], b"export MESSAGE='hello world'\necho $MESSAGE"
		)

	def test_a_guest_connection_runs_through_the_host_proxy_command(self) -> None:
		host = SSHRunner("fdab:1::7")
		proxy_command = host.get_proxy_command("ip netns exec metal-vm-00001 nc 172.16.0.2 22")
		runner = SSHRunner("vm-00001", proxy_command=proxy_command)
		process = Mock()
		process.wait.return_value = 0

		with (
			patch("atlas.atlas.core.ssh.subprocess.Popen", return_value=process) as popen,
			patch.object(runner, "_read_output", return_value=""),
		):
			runner.run_command("true")

		arguments = popen.call_args.args[0]
		self.assertIn(f"ProxyCommand={proxy_command}", arguments)
		self.assertEqual(arguments[-2], "root@vm-00001")
		self.assertTrue(
			proxy_command.endswith("root@fdab:1::7 'ip netns exec metal-vm-00001 nc 172.16.0.2 22'")
		)

	def test_run_script_loads_a_script_file(self) -> None:
		runner = SSHRunner("203.0.113.1")
		with patch.object(runner, "_run", return_value=SSHResult("", 0)) as run:
			runner.run_script("scaleway/configure-private-network.sh", data={"INTERFACE": "eno1"})

		self.assertIn("netplan generate", run.call_args.args[0])

	def test_read_output_sends_each_chunk_to_the_callback(self) -> None:
		process = subprocess.Popen(
			["printf", "one\\ntwo\\n"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
		)
		output: list[str] = []

		result = SSHRunner._read_output(process, 10, output.append)

		self.assertEqual(result, "one\ntwo\n")
		self.assertEqual("".join(output), result)

	def test_run_script_rejects_a_path(self) -> None:
		runner = SSHRunner("203.0.113.1")

		with self.assertRaises(ValueError):
			runner.run_script("../configure-private-network.sh")

	def test_wait_for_server_returns_the_first_available_user(self) -> None:
		runner = Mock()
		runner.run_command.side_effect = (SSHResult("", 1), SSHResult("", 0))

		with (
			patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner),
			patch("atlas.atlas.core.ssh.sleep"),
		):
			user = wait_for_server(
				host="203.0.113.1",
				users=("root", "ubuntu"),
				timeout_seconds=30,
				poll_interval_seconds=1,
			)

		self.assertEqual(user, "ubuntu")
