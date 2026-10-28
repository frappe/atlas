from __future__ import annotations

import frappe
from frappe.tests import UnitTestCase

from atlas.operations.investigator.command_policy import validate_read_only_command


class TestReadOnlyCommand(UnitTestCase):
	def test_allows_read_only_commands(self) -> None:
		for command in (
			"uptime",
			"journalctl -u metal.service --since -30min | grep -i error | tail -n 50",
			"df -h; free -m && ss -s",
			"systemctl status metal.service --no-pager",
			"ip -br addr",
			"ip route show table all",
			"ip netns list",
			"wg show all latest-handshakes",
			"zpool status -x",
			"cut -f 1 /proc/loadavg",
			"ps -eo pid,rss,cmd --sort=-rss | head",
			"grep 'set' /var/log/syslog",
		):
			with self.subTest(command=command):
				validate_read_only_command(command)

	def test_refuses_commands_that_change_or_hide_work(self) -> None:
		for command in (
			"",
			"rm -rf /tmp/x",
			"systemctl restart metal.service",
			"ip route flush table main",
			"ip link set wg0 down",
			"ip netns exec metal-vm-1 sh",
			"wg set wg0 peer abc remove",
			"zpool set autoexpand=on tank",
			"tail -f /var/log/syslog",
			"journalctl --vacuum-time=1d",
			"dmesg -C",
			"sort -o /etc/passwd x",
			"ss -K dst 10.0.0.1",
			"cat /etc/shadow",
			"cat /etc/wireguard/wg0.conf",
			"cat /proc/1/environ",
			"uptime > /tmp/out",
			"uptime & sleep 1",
			"echo $(id)",
			"ls `id`",
			"FOO=1 uptime",
			"/usr/bin/uptime",
			"uptime\nreboot",
		):
			with self.subTest(command=command), self.assertRaises(frappe.ValidationError):
				validate_read_only_command(command)
