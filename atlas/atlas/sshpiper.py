"""Dedicated SSHPiper ingress-VM control plane.

An SSHPiper gateway is an ordinary operator-owned Virtual Machine built from the
`sshpiper` image recipe and given a Reserved IPv4. Atlas configures it over guest
SSH on port 222; public port 22 belongs to sshpiperd. The gateway calls the
lookup below with its own VM-scoped token and can resolve uniquely-titled running
tenant VMs on the same compute Server.
"""

from __future__ import annotations

import hmac

import frappe
from frappe.utils import get_url
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from atlas.atlas._ssh.transport import Connection, run_ssh, ssh_key_file
from atlas.atlas.proxy import _record_guest_task, _write_guest_file
from atlas.atlas.ssh import connection_for_guest, connection_for_server

_TOKEN_HEADER = "X-Atlas-SSHPiper-Token"
_ENV_FILE = "/etc/atlas/sshpiper.env"
_KEY_FILE = "/etc/atlas/server_id_ed25519"


def build_sshpiper(virtual_machine: str) -> None:
	"""Build the committed SSHPiper tree inside an ingress build VM."""
	from atlas.atlas.image_builder import run_build
	from atlas.atlas.image_recipes import get_recipe

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if not vm.is_sshpiper:
		frappe.throw(f"Virtual Machine {virtual_machine} is not an SSHPiper gateway")
	run_build(virtual_machine, get_recipe("sshpiper"), stream=True)


def configure_gateway(virtual_machine: str) -> str:
	"""Install per-gateway runtime secrets and enable the baked service.

	The image contains binaries and the unit, never credentials. The gateway gets
	only its compute Server's private key; that Server's guests already trust the
	matching public key injected by provision-vm.py.
	"""

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if not vm.is_sshpiper:
		frappe.throw(f"Virtual Machine {virtual_machine} is not marked is_sshpiper")
	if vm.status != "Running":
		frappe.throw(f"SSHPiper gateway {virtual_machine} must be Running")
	if not vm.public_ipv4:
		frappe.throw(f"Attach a Reserved IPv4 to {virtual_machine} before configuring SSHPiper")

	token = _ensure_gateway_token(vm.name)
	private_key = _read_server_private_key(vm.server)
	base = connection_for_guest(vm)
	connection = Connection(
		host=base.host,
		ssh_private_key=base.ssh_private_key,
		user=base.user,
		port=222,
	)
	env = (
		f'ATLAS_URL="{get_url().rstrip("/")}"\n'
		+ f'SSHPIPER_GATEWAY="{vm.name}"\\n'
		+ f'SSHPIPER_API_KEY="{token}"\\n'
	)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		_write_guest_file(connection, key_path, _KEY_FILE, private_key, "0600", make_dir="/etc/atlas")
		_write_guest_file(connection, key_path, _ENV_FILE, env, "0600", make_dir="/etc/atlas")
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			"systemctl enable --now sshpiper.service && systemctl is-active sshpiper.service",
			timeout_seconds=60,
		)
	_record_guest_task(vm.name, "sshpiper-configure", {"public_ipv4": vm.public_ipv4}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"Configuring SSHPiper on {vm.name} failed (exit {code}): {stderr[-500:]}")
	vm.db_set("sshpiper_configured", 1)
	return vm.name


def _read_server_private_key(server_name: str) -> str:
	"""Read the gateway's compute-Server key without recording it in a Task."""
	server = frappe.get_doc("Server", server_name)
	connection = connection_for_server(server)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			"cat /root/.ssh/id_ed25519",
			timeout_seconds=30,
		)
	if code != 0 or not stdout.strip():
		frappe.throw(f"Reading SSHPiper key from Server {server_name} failed: {stderr[-300:]}")
	return stdout.strip() + "\n"


def _ensure_gateway_token(virtual_machine: str) -> str:
	token = get_decrypted_password(
		"Virtual Machine", virtual_machine, "sshpiper_api_key", raise_exception=False
	)
	if token:
		return token
	token = frappe.generate_hash(length=48)
	set_encrypted_password("Virtual Machine", virtual_machine, token, "sshpiper_api_key")
	return token


@frappe.whitelist(allow_guest=True)
def lookup_virtual_machine_ssh(gateway: str, vm_name: str, api_key: str | None = None) -> dict:
	"""Resolve one uniquely-titled running VM for an authenticated gateway."""
	if not gateway:
		frappe.throw("gateway is required")
	if not vm_name:
		frappe.throw("vm_name is required")
	token = _request_api_key(api_key)
	expected = get_decrypted_password(
		"Virtual Machine", gateway, "sshpiper_api_key", raise_exception=False
	)
	gateway_vm = frappe.db.get_value(
		"Virtual Machine", gateway, ["is_sshpiper", "server"], as_dict=True
	)
	if (
		not token
		or not gateway_vm
		or not gateway_vm.is_sshpiper
		or not expected
		or not hmac.compare_digest(str(token), str(expected))
	):
		frappe.throw("Not permitted", frappe.PermissionError)

	matches = frappe.get_all(
		"Virtual Machine",
		filters={"title": vm_name, "status": "Running", "is_sshpiper": 0, "server": gateway_vm.server},
		fields=["name", "title", "server", "ipv6_address", "ssh_public_key"],
		limit=2,
	)
	if len(matches) != 1:
		frappe.throw("Not permitted", frappe.PermissionError)
	vm = matches[0]
	if not vm.ipv6_address:
		frappe.throw(f"Virtual Machine {vm_name} has no ipv6_address")
	return {
		"virtual_machine": vm.name,
		"title": vm.title,
		"server": vm.server,
		"ipv6_address": vm.ipv6_address,
		"host": vm.ipv6_address,
		"public_keys": _public_key_lines(vm.ssh_public_key),
	}


def _request_api_key(api_key: str | None = None) -> str:
	if api_key:
		return api_key
	header = frappe.get_request_header(_TOKEN_HEADER) or ""
	if header:
		return header.strip()
	authorization = frappe.get_request_header("Authorization") or ""
	return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


def _public_key_lines(value: str | None) -> list[str]:
	return [
		line
		for line in (line.strip() for line in (value or "").splitlines())
		if line and not line.startswith("#")
	]
