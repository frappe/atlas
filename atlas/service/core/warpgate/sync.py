from __future__ import annotations

import subprocess
from functools import cached_property

import frappe

from atlas.atlas.core.ssh import SSHRunner
from atlas.service.core.warpgate.client import WarpgateClient, WarpgateError

ALL_HOSTS_ROLE = "all-hosts"
HOST_ROLE_PREFIX = "host:"
FAILURE_CACHE_KEY = "atlas:warpgate-sync-failure"
FAILURE_REPEAT_SECONDS = 3600
# Root on the host trusts these keys, and Warpgate learns the host key, in one SSH call.
TRUST_HOST_SCRIPT = """set -eu
install -d -m 0700 /root/.ssh
touch /root/.ssh/authorized_keys
printf '%s\\n' "$WARPGATE_KEYS" | while IFS= read -r key; do
	grep -qxF "$key warpgate" /root/.ssh/authorized_keys || printf '%s warpgate\\n' "$key" >> /root/.ssh/authorized_keys
done
cat /etc/ssh/ssh_host_ed25519_key.pub
"""
# The target description names the Metal Server, so a renamed host keeps its target and role.
MANAGED_DESCRIPTION_PREFIX = "Managed by Atlas: "


def get_host_role(title: str) -> str:
	return f"{HOST_ROLE_PREFIX}{title}"


class WarpgateTargetSync:
	"""Make the Warpgate targets and roles of this region match its Metal Servers."""

	def __init__(self, client: WarpgateClient) -> None:
		self.client = client

	@staticmethod
	def get_target_data(server: str, title: str, wireguard_ip_address: str) -> dict:
		"""Return the Warpgate target for one host. Warpgate needs every field on each write."""
		return {
			"name": title,
			"description": f"{MANAGED_DESCRIPTION_PREFIX}{server}",
			"options": {
				"kind": "Ssh",
				"host": wireguard_ip_address,
				"port": 22,
				"username": "root",
				"allow_insecure_algos": False,
				"auth": {"kind": "PublicKey"},
			},
			"ticket_requests_disabled": True,
			"ticket_require_approval": False,
			"require_approval": False,
		}

	def run(self) -> None:
		"""Sync every host, then fail with every host that failed."""
		hosts = self.get_hosts()
		self.roles = {role["name"]: role["id"] for role in self.client.list_roles()}
		managed = {
			target["description"].removeprefix(MANAGED_DESCRIPTION_PREFIX): target
			for target in self.client.list_targets()
			if (target.get("description") or "").startswith(MANAGED_DESCRIPTION_PREFIX)
		}

		known_hosts = {known_host["host"] for known_host in self.client.list_known_hosts()}

		failures = []
		for server, (title, wireguard_ip_address, status) in hosts.items():
			try:
				self.sync_host(server, title, wireguard_ip_address, managed.get(server))
				if status == "Running" and wireguard_ip_address not in known_hosts:
					self.trust_host(wireguard_ip_address)
			except WarpgateError as error:
				failures.append(f"{title}: {error}")

		for server in managed.keys() - hosts.keys():
			target = managed[server]
			self.client.delete_target(target["id"])
			if role_id := self.roles.get(get_host_role(target["name"])):
				self.client.delete_role(role_id)

		self.report_failures(failures)

	def sync_host(self, server: str, title: str, wireguard_ip_address: str, target: dict | None) -> None:
		"""Create or update the target of one host, follow a rename, and attach its two roles."""
		data = self.get_target_data(server, title, wireguard_ip_address)
		role = get_host_role(title)
		if target is None:
			target = self.client.create_target(data)
			attached = set()
		else:
			if target["name"] != title or target["options"].get("host") != wireguard_ip_address:
				self.client.update_target(target["id"], data)
			old_role = get_host_role(target["name"])
			if target["name"] != title and old_role in self.roles:
				# Grants point at the role ID, so renaming the role keeps them.
				self.client.rename_role(self.roles[old_role], role)
				self.roles[role] = self.roles.pop(old_role)
			attached = {attached_role["id"] for attached_role in self.client.list_target_roles(target["id"])}

		for name in (ALL_HOSTS_ROLE, role):
			if name not in self.roles:
				self.roles[name] = self.client.create_role(name)["id"]
			if self.roles[name] not in attached:
				self.client.add_target_role(target["id"], self.roles[name])

	@cached_property
	def public_keys(self) -> list[str]:
		return self.client.get_public_keys()

	def trust_host(self, wireguard_ip_address: str) -> None:
		"""Let Warpgate log in as root on a new host, and pin its host key."""
		try:
			result = SSHRunner(wireguard_ip_address).run_command(
				TRUST_HOST_SCRIPT, data={"WARPGATE_KEYS": "\n".join(self.public_keys)}, timeout_seconds=60
			)
		except (OSError, subprocess.TimeoutExpired) as error:
			raise WarpgateError(f"Could not reach host {wireguard_ip_address}: {error}") from error
		fields = result.output.strip().splitlines()[-1].split() if result.output.strip() else []
		if not result.is_success or len(fields) < 2 or fields[0] != "ssh-ed25519":
			raise WarpgateError(
				f"Could not prepare host {wireguard_ip_address}: {result.output.strip()[-300:]}"
			)
		self.client.add_known_host(wireguard_ip_address, fields[0], fields[1])

	@staticmethod
	def report_failures(failures: list[str]) -> None:
		"""Fail the run so the scheduler logs it, at most once an hour for the same failure."""
		if not failures:
			frappe.cache.delete_value(FAILURE_CACHE_KEY)
			return

		message = "Warpgate target sync failed for " + "; ".join(failures)
		if frappe.cache.get_value(FAILURE_CACHE_KEY) == message:
			return
		frappe.cache.set_value(FAILURE_CACHE_KEY, message, expires_in_sec=FAILURE_REPEAT_SECONDS)
		raise WarpgateError(message)

	@staticmethod
	def get_hosts() -> dict[str, tuple[str, str, str]]:
		"""Return the title, wg0 address, and status of each reachable host, by Metal Server name."""
		return {
			name: (title, wireguard_ip_address, status)
			for name, title, wireguard_ip_address, status in frappe.get_all(
				"Metal Server",
				filters={
					"status": ["!=", "Deleted"],
					"wireguard_public_key": ["is", "set"],
					"wireguard_ip_address": ["is", "set"],
				},
				fields=["name", "title", "wireguard_ip_address", "status"],
				as_list=True,
			)
		}


def sync_warpgate_targets() -> None:
	"""Scheduler entry point. A region without Warpgate does nothing."""
	client = WarpgateClient.from_settings()
	if client:
		WarpgateTargetSync(client).run()
