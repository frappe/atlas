from __future__ import annotations

import shlex

import frappe

from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address


def proxy_route() -> tuple[str, str]:
	"""Return an operator-only HTTPS virtual host and its upstream trust anchor."""
	cache = frappe.get_single("Runway Cache Server")
	if cache.status not in ("Provisioning", "Active") or not cache.virtual_machine or not cache.operator_ca:
		return "", ""
	address = get_virtual_machine_mesh_address(frappe._dict(name=cache.virtual_machine, tenant_id=0))
	return (
		f"""server {{
    listen 127.0.0.1:8443 ssl proxy_protocol;
    listen [::1]:8443 ssl proxy_protocol;
    server_name {cache.domain};
    location = /configure {{
        limit_except POST {{ deny all; }}
        proxy_pass https://[{address}]:{cache.operator_port};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/nginx/runway-cache/ca.pem;
        proxy_ssl_server_name on;
        proxy_ssl_name {cache.domain};
        proxy_set_header Host {cache.domain};
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }}
    location / {{ return 404; }}
}}
""",
		cache.operator_ca,
	)


def write_proxy_route(route: str, ca: str) -> str:
	"""Write the public route inputs without placing credentials in an SSH Task."""
	return (
		"\ninstall -d -m 0750 /etc/nginx/runway-cache\n"
		f"printf %s {shlex.quote(ca)} > /etc/nginx/runway-cache/ca.pem\n"
		f"printf %s {shlex.quote(route)} > /etc/nginx/runway-cache/operator.conf\n"
	)
