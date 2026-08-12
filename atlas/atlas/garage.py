from __future__ import annotations

import hmac

import frappe
from frappe.utils import get_url
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from atlas.atlas._ssh.transport import Connection, run_ssh, ssh_key_file
from atlas.atlas.proxy import _record_guest_task, _write_guest_file
from atlas.atlas.ssh import connection_for_guest, connection_for_server
from secrets import token_hex, token_urlsafe

_CONFIG_FILE = "/etc/garage.toml"
_NGINX_CONFIG = "/etc/nginx/conf.d/garage.conf"


def build_garage(virtual_machine: str) -> None:
	"""Build the committed Garage tree inside an ingress build VM."""
	from atlas.atlas.image_builder import run_build
	from atlas.atlas.image_recipes import get_recipe

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if not vm.is_garage:
		frappe.throw(f"Virtual Machine {virtual_machine} is not a garage instance")
	run_build(virtual_machine, get_recipe("garage"), stream=True)


def configure_garage(virtual_machine: str) -> str:
	"""Install per-garage runtime secrets and enable the baked service.

	The image contains binaries and the unit, never credentials	"""

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if not vm.is_garage:
		frappe.throw(f"Virtual Machine {virtual_machine} is not marked is_garage")
	if vm.status != "Running":
		frappe.throw(f"Garage {virtual_machine} must be Running")

	base = connection_for_guest(vm)
	connection = Connection(
		host=base.host,
		ssh_private_key=base.ssh_private_key,
		user=base.user,
		port=22,
	)

	region = frappe.db.get_single_value("Atlas Settings", "region")

	garage = frappe.get_doc("Garage Settings")


	bootstrap_peers = [
	    row.peer_id
	    for row in (garage.gateway_machines + garage.data_machines)
	    if row.peer_id and row.peer_id != vm.peer_id
	]

	nginx_conf = f"""
	server {{
		listen 80;
        listen [::]:80;
		server_name {garage.api_domain};
		location / {{
			proxy_pass http://127.0.0.1:3900;
			proxy_set_header Host $host;
			proxy_set_header X-Real-IP $remote_addr;
		}}
	}}
	server {{
		listen 80;
        listen [::]:80;
		server_name {garage.web_domain}
		location / {{
			proxy_pass http://127.0.0.1:3902
			proxy_set_header Host $host;
			proxy_set_header X-Real-IP $remote_addr;
		}}
	}}

	"""
	garage_toml = _generate_garage_config(garage.num_nodes, vm.garage_type, bootstrap_peers, vm.ipv6_address, garage.rpc_secret,
									  garage.admin_secret, garage.metrics_secret, region,
									  garage.api_domain, garage.web_domain)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		_write_guest_file(connection, key_path, _CONFIG_FILE, garage_toml, "0600")
		if vm.garage_type == "gateway":
			_write_guest_file(connection, key_path, _NGINX_CONFIG, nginx_conf, "0600")
		command = f"""systemctl enable --now garage.service { "nginx.service" if vm.garage_type == "gateway" else "" } && \
		until garage status >/dev/null 2>&1; do
		    sleep 2
		done && \
		garage layout assign \
		    -z {vm.server} \
		    {"-c \"$(df -B1 --output=avail /var/lib/garage/data | tail -1)\"" if vm.garage_type == "data" else "--gateway"} \
		    "$(garage node id -q | cut -d@ -f1)"
		"""

		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			command,
			timeout_seconds=60,
		)
		stdout1, stderr1, code1 = run_ssh(
			connection,
			key_path,
			"garage node id -q",
			timeout_seconds=60,
		)
	_record_guest_task(vm.name, "garage-configure", {}, stdout, stderr, code)
	_record_guest_task(vm.name, "garage-get-peer-id", {}, stdout1, stderr1, code1)
	if code != 0:
		frappe.throw(f"Configuring garage on {vm.name} failed (exit {code}): {stderr[-500:]}")
	if code1 != 0:
		frappe.throw(f"Could not get peer id for {vm.name} (exit {code1}): {stderr1[-500:]}")
	vm.db_set("peer_id", stdout1)
	vm.db_set("garage_configured", 1)
	table = f"{vm.garage_type}_machines"

	if not any(row.virtual_machine == vm.name for row in getattr(garage, table)):
	    garage.append(table, {
	        "virtual_machine": vm.name,
	    })
	    garage.save(ignore_permissions=True)
	return vm.name
def _generate_garage_config(
	num_nodes: int,
	garage_type: str,
    bootstrap_peers: list[str],
    public_ipv6: str,
    rpc_secret: str,
    admin_token: str,
    metrics_token: str,
    s3_region: str,
    api_domain: str,
    web_domain: str,
):
	if bootstrap_peers:
	    bootstrap_peers_config = (
	        "bootstrap_peers = [\n"
	        + "".join(f'    "{peer.strip()}",\n' for peer in bootstrap_peers)
	        + "]"
	    )
	else:
	    bootstrap_peers_config = ""
	gateway_config = f"""
	[s3_api]
	api_bind_addr = "[::]:3900"
	s3_region = "{s3_region}"
	root_domain = ".{api_domain}"

	[s3_web]
	bind_addr = "[::]:3902"
	root_domain = ".{web_domain}"
	add_host_to_metrics = true
	"""

	data_config = f"""
	[s3_api]
	api_bind_addr = "127.0.0.1:3900"
	s3_region = "{s3_region}"
	root_domain = ".{api_domain}"

	[s3_web]
	bind_addr = "127.0.0.1:3902"
	root_domain = ".{web_domain}"
	add_host_to_metrics = true
	"""

	config = f"""
	replication_factor = {num_nodes}
	consistency_mode = "consistent"

	metadata_dir = "/var/lib/garage/meta"
	data_dir = "/var/lib/garage/data"
	metadata_snapshots_dir = "/var/lib/garage/snapshots"

	metadata_fsync = true
	data_fsync = false

	disable_scrub = false
	use_local_tz = false
	metadata_auto_snapshot_interval = "6h"

	db_engine = "lmdb"

	block_size = "1M"
	block_ram_buffer_max = "256MiB"
	block_max_concurrent_reads = 16
	block_max_concurrent_writes_per_request = 10
	lmdb_map_size = "1T"

	compression_level = 1

	rpc_secret = "{rpc_secret}"
	rpc_bind_addr = "[::]:3901"
	rpc_bind_outgoing = false
	rpc_public_addr = "[{public_ipv6}]:3901"

	allow_world_readable_secrets = false

	{bootstrap_peers_config}

	allow_punycode = false

	{gateway_config if garage_type == "gateway" else data_config}

	[admin]
	api_bind_addr = "0.0.0.0:3903"
	metrics_token = "{metrics_token}"
	metrics_require_token = true
	admin_token = "{admin_token}"
	"""
	return config
