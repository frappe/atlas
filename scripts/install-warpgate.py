#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

VERSION = "v0.29.1"
DOWNLOAD_URL = (
	f"https://github.com/warp-tech/warpgate/releases/download/{VERSION}/warpgate-{VERSION}-x86_64-linux"
)
SHA256 = "470534bf570b2450dfce646c7e09bdd77b792db83f9865dc8eeca99ce70fc6bd"
BINARY = Path("/usr/local/bin/warpgate")
DATA = Path("/var/lib/warpgate")
CONFIG = Path("/etc/warpgate.yaml")
# Outside DATA, which belongs to the warpgate user.
ADMIN_PASSWORD_FILE = Path("/root/warpgate-admin-password")
PASSWORD_FILE = Path("/root/warpgate-atlas-password")
NGINX_CONFIG = Path("/etc/nginx/conf.d/warpgate.conf")
UNIT = Path("/etc/systemd/system/warpgate.service")
USER = "warpgate"
HTTP_PORT = 8888
SSH_PORT = 2223
URL = f"https://127.0.0.1:{HTTP_PORT}"
ADMIN_API = "/@warpgate/admin/api"
SSO_PROVIDER = "central"
TOKEN_LIFETIME = timedelta(days=365)
# Warpgate puts known host keys under config_edit.
ATLAS_PERMISSIONS = {
	"targets_create": True,
	"targets_edit": True,
	"targets_delete": True,
	"users_create": True,
	"users_edit": True,
	"users_delete": False,
	"access_roles_create": True,
	"access_roles_edit": True,
	"access_roles_delete": True,
	"access_roles_assign": True,
	"sessions_view": True,
	"sessions_terminate": True,
	"approve_sessions": False,
	"recordings_view": False,
	"tickets_create": False,
	"tickets_delete": False,
	"config_edit": True,
	"admin_roles_manage": False,
	"ticket_requests_manage": False,
}
UNIT_FILE = f"""[Unit]
Description=Warpgate SSH bastion
After=network-online.target
Wants=network-online.target

[Service]
User={USER}
ExecStart={BINARY} --config {CONFIG} run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
NGINX_SERVER = """server {{
	listen 443 ssl;
	listen [::]:443 ssl;
	server_name {name};
	ssl_certificate {certificate};
	ssl_certificate_key {key};

	location / {{
		proxy_pass {url};
		proxy_ssl_verify off;
		proxy_http_version 1.1;
		proxy_set_header Upgrade $http_upgrade;
		proxy_set_header Connection "upgrade";
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $remote_addr;
		proxy_set_header X-Forwarded-Proto https;
	}}
}}
"""


def step(message: str) -> None:
	print(f"==> {message}", file=sys.stderr, flush=True)


def run(command: list[str], **keywords) -> subprocess.CompletedProcess:
	return subprocess.run(command, check=True, **keywords)


def write_root_file(path: Path, content: str) -> None:
	with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
		file.write(content)


def loopback_context() -> ssl.SSLContext:
	# Warpgate serves its own certificate on loopback only.
	context = ssl.create_default_context()
	context.check_hostname = False
	context.verify_mode = ssl.CERT_NONE
	return context


class Session:
	"""One logged-in Warpgate HTTP session."""

	def __init__(self, username: str, password: str) -> None:
		self.opener = urllib.request.build_opener(
			urllib.request.HTTPSHandler(context=loopback_context()),
			urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
		)
		self.call("POST", "/@warpgate/api/auth/login", {"username": username, "password": password})

	def call(self, method: str, path: str, body: dict | None = None) -> dict | None:
		request = urllib.request.Request(
			URL + path,
			data=json.dumps(body).encode() if body is not None else None,
			method=method,
			headers={"Content-Type": "application/json"},
		)
		with self.opener.open(request, timeout=30) as response:
			content = response.read()
		return json.loads(content) if content else None

	def find(self, path: str, key: str, value: str) -> dict | None:
		return next((item for item in self.call("GET", ADMIN_API + path) if item[key] == value), None)


class WarpgateInstaller:
	"""Install and configure Warpgate for one region, and issue the Atlas token."""

	def __init__(
		self, site_path: Path, wildcard_domain: str, issuer_url: str, client_id: str, client_secret: str
	) -> None:
		self.site_path = site_path
		self.external_host = f"warpgate.{wildcard_domain}"
		self.issuer_url = issuer_url
		self.client_id = client_id
		self.client_secret = client_secret

	def run(self) -> dict[str, str]:
		"""Converge the install and issue a new token. A failed run can run again."""
		is_changed = self.install_binary()
		self.first_setup()
		is_changed = self.write_config() or is_changed
		self.start_service(is_changed)
		if not PASSWORD_FILE.exists():
			self.create_atlas_user()
		self.configure_nginx()
		return {"warpgate_url": URL, **self.issue_token()}

	def install_binary(self) -> bool:
		if BINARY.exists() and hashlib.sha256(BINARY.read_bytes()).hexdigest() == SHA256:
			return False
		step(f"download Warpgate {VERSION}")
		with urllib.request.urlopen(DOWNLOAD_URL, timeout=300) as response:
			data = response.read()
		if hashlib.sha256(data).hexdigest() != SHA256:
			raise SystemExit(f"Warpgate {VERSION} does not match its pinned SHA-256")
		with tempfile.NamedTemporaryFile(dir=BINARY.parent, delete=False) as staged:
			staged.write(data)
		os.chmod(staged.name, 0o755)
		os.replace(staged.name, BINARY)
		return True

	def first_setup(self) -> None:
		"""Create the database, keys, and certificate once. A new database drops the old atlas password."""
		if (DATA / "db").exists():
			return
		step("first Warpgate setup")
		if subprocess.run(["id", USER], capture_output=True).returncode != 0:
			run(["useradd", "--system", "--home-dir", str(DATA), "--shell", "/usr/sbin/nologin", USER])
		DATA.mkdir(mode=0o700, exist_ok=True)
		password = secrets.token_urlsafe(32)
		write_root_file(ADMIN_PASSWORD_FILE, password)
		PASSWORD_FILE.unlink(missing_ok=True)
		run(
			[
				str(BINARY),
				"--config",
				str(CONFIG),
				"unattended-setup",
				"--data-path",
				str(DATA),
				"--http-port",
				str(HTTP_PORT),
				"--ssh-port",
				str(SSH_PORT),
				"--record-sessions",
				"--host-key-verification",
				"auto-reject",
				"--external-host",
				self.external_host,
			],
			env={**os.environ, "WARPGATE_ADMIN_PASSWORD": password},
			stdout=subprocess.DEVNULL,
		)

	def build_config(self, data: Path = DATA) -> dict:
		"""Return the config. Only SSH and loopback HTTP listen."""
		certificate, key = str(data / "tls.certificate.pem"), str(data / "tls.key.pem")
		disabled = {"enable": False, "certificate": certificate, "key": key}
		return {
			"external_host": self.external_host,
			"database_url": f"sqlite:{data / 'db'}",
			"ssh": {
				"enable": True,
				"listen": f"[::]:{SSH_PORT}",
				"external_port": SSH_PORT,
				"host_key_verification": "auto_reject",
			},
			"http": {
				"listen": f"127.0.0.1:{HTTP_PORT}",
				"external_port": 443,
				"certificate": certificate,
				"key": key,
				"trust_x_forwarded_headers": True,
			},
			"mysql": disabled,
			"postgres": disabled,
			"kubernetes": disabled,
			"vnc": disabled,
			"rdp": disabled,
			"sso_providers": [
				{
					"name": SSO_PROVIDER,
					"label": "Frappe Central",
					"auto_create_users": False,
					"provider": {
						"type": "custom",
						"issuer_url": self.issuer_url,
						"client_id": self.client_id,
						"client_secret": self.client_secret,
						"scopes": ["openid", "email", "profile"],
						"admin_roles_claim": "roles",
						"admin_role_mappings": {"Atlas Warpgate Admin": "warpgate:admin"},
					},
				}
			],
		}

	def write_config(self) -> bool:
		"""Write the config when it changed, and check it with Warpgate. JSON is valid YAML."""
		content = json.dumps(self.build_config(), indent=1) + "\n"
		if CONFIG.exists() and CONFIG.read_text() == content:
			return False
		CONFIG.write_text(content)
		shutil.chown(CONFIG, USER, USER)
		os.chmod(CONFIG, 0o600)
		run([str(BINARY), "--config", str(CONFIG), "check"], stdout=subprocess.DEVNULL)
		return True

	def start_service(self, is_changed: bool) -> None:
		run(["chown", "-R", f"{USER}:{USER}", str(DATA)])
		if not UNIT.exists() or UNIT.read_text() != UNIT_FILE:
			UNIT.write_text(UNIT_FILE)
			run(["systemctl", "daemon-reload"])
			is_changed = True
		run(["systemctl", "enable", "--quiet", "warpgate"])
		run(["systemctl", "restart" if is_changed else "start", "warpgate"])
		deadline = time.monotonic() + 60
		while time.monotonic() < deadline:
			try:
				urllib.request.urlopen(f"{URL}/@warpgate", context=loopback_context(), timeout=5)
				return
			except OSError:
				time.sleep(1)
		raise SystemExit("Warpgate did not start; read: journalctl -u warpgate")

	def create_atlas_user(self) -> None:
		"""Create the atlas user with the atlas-sync admin role, reusing what a failed run created."""
		step("Warpgate atlas user")
		admin = Session("admin", ADMIN_PASSWORD_FILE.read_text().strip())
		role_data = {"name": "atlas-sync", **ATLAS_PERMISSIONS}
		role = admin.find("/admin-roles", "name", "atlas-sync")
		if role:
			admin.call("PUT", f"{ADMIN_API}/admin-roles/{role['id']}", role_data)
		else:
			role = admin.call("POST", f"{ADMIN_API}/admin-roles", role_data)
		user = admin.find("/users", "username", "atlas") or admin.call(
			"POST", f"{ADMIN_API}/users", {"username": "atlas"}
		)
		if not admin.find(f"/users/{user['id']}/admin-roles", "id", role["id"]):
			admin.call("POST", f"{ADMIN_API}/users/{user['id']}/admin-roles/{role['id']}")
		password = secrets.token_urlsafe(32)
		admin.call("POST", f"{ADMIN_API}/users/{user['id']}/credentials/passwords", {"password": password})
		write_root_file(PASSWORD_FILE, password)

	def issue_token(self) -> dict[str, str]:
		"""Return a new API token of the atlas user and its ID. Atlas deletes the others."""
		expiry = (datetime.now(UTC) + TOKEN_LIFETIME).isoformat()
		token = Session("atlas", PASSWORD_FILE.read_text().strip()).call(
			"POST", "/@warpgate/api/profile/api-tokens", {"label": "atlas", "expiry": expiry}
		)
		return {"warpgate_api_token": token["secret"], "warpgate_api_token_id": token["token"]["id"]}

	def configure_nginx(self) -> None:
		"""Serve the UI. The Warpgate certificate holds the place until Atlas writes the wildcard one."""
		tls = self.site_path / "private" / "warpgate"
		certificate, key = tls / "tls.crt", tls / "tls.key"
		if not certificate.exists():
			owner = self.site_path.stat()
			tls.mkdir(mode=0o700, parents=True, exist_ok=True)
			certificate.write_bytes((DATA / "tls.certificate.pem").read_bytes())
			key.write_bytes((DATA / "tls.key.pem").read_bytes())
			for path in (tls, certificate, key):
				os.chown(path, owner.st_uid, owner.st_gid)
			os.chmod(key, 0o600)
		content = NGINX_SERVER.format(name=self.external_host, certificate=certificate, key=key, url=URL)
		if NGINX_CONFIG.exists() and NGINX_CONFIG.read_text() == content:
			return
		NGINX_CONFIG.write_text(content)
		run(["nginx", "-t"], capture_output=True)
		run(["systemctl", "reload", "nginx"])


def main() -> None:
	"""Install Warpgate as root, and print the URL and a new token for configure-atlas. Safe to run again."""
	parser = argparse.ArgumentParser(
		description="Install Warpgate in the Atlas VM. Reads the OIDC client secret from WARPGATE_OIDC_CLIENT_SECRET."
	)
	parser.add_argument("--site-path", type=Path, required=True)
	parser.add_argument("--wildcard-domain", required=True)
	parser.add_argument("--issuer-url", required=True)
	parser.add_argument("--client-id", required=True)
	arguments = parser.parse_args()
	client_secret = os.environ.get("WARPGATE_OIDC_CLIENT_SECRET")
	if not client_secret:
		raise SystemExit("WARPGATE_OIDC_CLIENT_SECRET is required")

	installer = WarpgateInstaller(
		arguments.site_path,
		arguments.wildcard_domain,
		arguments.issuer_url,
		arguments.client_id,
		client_secret,
	)
	print(json.dumps(installer.run()))


if __name__ == "__main__":
	main()
