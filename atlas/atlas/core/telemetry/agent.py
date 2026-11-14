from __future__ import annotations

from pathlib import Path

from datum_client.agent import get_install_script

from atlas.atlas.core.ssh import SSHResult, SSHRunner
from atlas.atlas.core.telemetry.datum import HOST_TOKEN_LIFETIME, get_datum_url, issue_datum_token

INSTALL_TIMEOUT_SECONDS = 600
ROLES = Path(__file__).parent / "agent_roles"


class DatumAgent:
	"""The Datum agent on one machine that Atlas runs: Vector with the machine metrics and logs of the
	telemetry contract, and the role file of the machine. The token goes over direct SSH, so no SSH Task
	stores it."""

	def __init__(self, runner: SSHRunner, resource_id: str, product: str) -> None:
		self.runner = runner
		self.resource_id = resource_id
		self.product = product

	def install(
		self,
		role: str | None = None,
		send_metrics: bool = True,
		info_units: tuple[str, ...] = (),
		prelude: str = "",
	) -> SSHResult:
		"""Install the agent, or rewrite its configuration with a new token. Safe to repeat."""
		environment = {
			"DATUM_ENDPOINT": get_datum_url(),
			"DATUM_TOKEN": issue_datum_token(self.resource_id, HOST_TOKEN_LIFETIME),
			"PRODUCT": self.product,
			"SEND_METRICS": str(send_metrics).lower(),
			"INFO_UNITS": " ".join(info_units),
			"ROLE_CONFIG": (ROLES / f"{role}.yaml").read_text() if role else "",
		}
		return self.runner.run(
			prelude + get_install_script(), data=environment, timeout_seconds=INSTALL_TIMEOUT_SECONDS
		)
