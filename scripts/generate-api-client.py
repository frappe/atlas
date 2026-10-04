"""Write the OpenAPI document and the Python client of one component into clients/."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLIENTS = ROOT / "clients"
GENERATOR = "openapi-python-client"

PROXY_CONFIGURATION = """[control]
domain = "proxy-001.example.com"

[tls]
wildcard_domain = "*.example.com"
fullchain_pem = "leaf"
private_key_pem = "key"
"""

WG_GATEWAY_CONFIGURATION = """[gateway]
region_id = 1
node_id = "wireguard-001"
private_key = "key"

[[nodes]]
node_id = "wireguard-001"
gateway_id = 1
endpoint = "wireguard-001.example.com"
listen_port = 51820
public_key = "key"

[auth]

[cluster]
node_id = "wireguard-001"
password = "password"
peers = [{ node_id = "wireguard-001", address = "https://wireguard-001.example.com" }]

[tls]
fullchain_pem = "leaf"
private_key_pem = "key"
"""


def build_atlas_specification() -> dict[str, Any]:
	"""Return the OpenAPI document of the Atlas tenant API."""
	from atlas.api.core.docs import generate_specification
	from atlas.api.router import atlas_router, register_atlas_api

	register_atlas_api()
	return generate_specification(atlas_router)


def build_http_proxy_specification() -> dict[str, Any]:
	"""Return the OpenAPI document of the HTTP proxy control daemon."""
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "proxy-control.toml"
		path.write_text(PROXY_CONFIGURATION)
		os.environ["ATLAS_PROXY_CONTROL_CONFIG"] = str(path)

		from proxy_control.main import app

		return app.openapi()


def build_wg_gateway_specification() -> dict[str, Any]:
	"""Return the OpenAPI document of the WireGuard gateway daemon."""
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "wireguard-gateway.toml"
		path.write_text(WG_GATEWAY_CONFIGURATION)
		os.environ["ATLAS_WG_GATEWAY_CONFIG"] = str(path)

		from gatewayd.main import app

		return app.openapi()


@dataclass(frozen=True)
class Client:
	"""One generated client and the specification that it comes from."""

	name: str
	package: str
	build_specification: Callable[[], dict[str, Any]]
	sort_keys: bool = True

	@property
	def specification_path(self) -> Path:
		"""Return the path of the OpenAPI document."""
		return CLIENTS / "openapi" / f"{self.name}.json"

	@property
	def path(self) -> Path:
		"""Return the path of the client package."""
		return CLIENTS / self.name

	def write_specification(self) -> None:
		"""Write the OpenAPI document of the component."""
		self.specification_path.parent.mkdir(parents=True, exist_ok=True)
		document = json.dumps(self.build_specification(), indent=2, sort_keys=self.sort_keys)
		self.specification_path.write_text(document + "\n")

	def write_client(self) -> None:
		"""Generate the Python client from the OpenAPI document."""
		with tempfile.TemporaryDirectory() as directory:
			configuration = Path(directory) / "config.yml"
			configuration.write_text(
				f"package_name_override: {self.package}\n"
				f"project_name_override: {self.name}\n"
				# No post hooks. The generated client is not linted or formatted.
				"post_hooks: []\n"
			)
			subprocess.run(
				[
					GENERATOR,
					"generate",
					"--path",
					str(self.specification_path),
					"--output-path",
					str(self.path),
					"--config",
					str(configuration),
					"--meta",
					"pdm",
					"--overwrite",
				],
				check=True,
			)


CLIENTS_BY_NAME = {
	client.name: client
	for client in (
		Client("atlas-client", "atlas_client", build_atlas_specification),
		Client("atlas-proxy-client", "atlas_proxy_client", build_http_proxy_specification),
		Client(
			"atlas-wg-gateway-client",
			"atlas_wg_gateway_client",
			build_wg_gateway_specification,
			sort_keys=False,
		),
	)
}


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("client", choices=sorted(CLIENTS_BY_NAME))
	arguments = parser.parse_args()

	client = CLIENTS_BY_NAME[arguments.client]
	client.write_specification()
	client.write_client()
	print(f"Wrote {client.specification_path.relative_to(ROOT)} and {client.path.relative_to(ROOT)}")


if __name__ == "__main__":
	sys.exit(main())
