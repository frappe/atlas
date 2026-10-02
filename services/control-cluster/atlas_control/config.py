import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAXIMUM_CLUSTER_MEMBERS = 5


class ConfigError(Exception):
	"""The configuration is invalid."""


@dataclass(frozen=True)
class AuthConfig:
	"""Caller credentials."""

	password_hash: str = ""
	previous_password_hash: str = ""
	previous_password_valid_until: int = 0
	jwks_url: str = ""
	jwks_audience_id: str = ""
	jwks_issuers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterPeer:
	"""One cluster member."""

	node_id: str
	address: str


@dataclass(frozen=True)
class ClusterConfig:
	"""Regional cluster membership and its shared password."""

	state_path: Path
	node_id: str = ""
	password: str = ""
	previous_password: str = ""
	previous_password_valid_until: int = 0
	peers: tuple[ClusterPeer, ...] = ()

	@property
	def is_enabled(self) -> bool:
		"""Report whether this node has cluster membership."""
		return bool(self.node_id and self.password and self.peers)


def read_toml(path: Path) -> dict[str, object]:
	"""Read one TOML file. A missing file is empty."""
	try:
		with path.open("rb") as source:
			return tomllib.load(source)
	except FileNotFoundError:
		return {}
	except OSError as error:
		raise ConfigError(f"cannot read {path}: {error}") from error
	except tomllib.TOMLDecodeError as error:
		raise ConfigError(f"{path} is not valid TOML: {error}") from error


def parse_auth(section: dict[str, object], audience_service: str) -> AuthConfig:
	"""Return the [auth] credentials. The audience must be `<audience_service>:<region ID>`."""
	jwks_issuers = section.get("jwks_issuers", [])
	if not isinstance(jwks_issuers, list) or not all(isinstance(value, str) for value in jwks_issuers):
		raise ConfigError("auth.jwks_issuers must be an array of strings")
	_validate_jwks_authority(section, jwks_issuers, audience_service)

	return AuthConfig(
		password_hash=text(section, "password_hash"),
		previous_password_hash=text(section, "previous_password_hash"),
		previous_password_valid_until=integer(section, "previous_password_valid_until"),
		jwks_url=text(section, "jwks_url"),
		jwks_audience_id=text(section, "jwks_audience_id"),
		jwks_issuers=tuple(jwks_issuers),
	)


def auth_loader(path: Path, audience_service: str) -> Callable[[], AuthConfig]:
	"""Return a reader for the current [auth] credentials. An invalid file grants nothing."""

	def load_auth() -> AuthConfig:
		try:
			return parse_auth(section(read_toml(path), "auth"), audience_service)
		except ConfigError:
			return AuthConfig()

	return load_auth


def parse_cluster(values: dict[str, object], default_state_path: Path) -> ClusterConfig:
	"""Return the [cluster] membership."""
	raw_peers = values.get("peers", [])
	if not isinstance(raw_peers, list):
		raise ConfigError("cluster.peers must be an array")

	peers = []
	for raw_peer in raw_peers:
		if not isinstance(raw_peer, dict):
			raise ConfigError("each cluster peer must be a table")
		node_id = text(raw_peer, "node_id")
		address = text(raw_peer, "address").rstrip("/")
		if not node_id or not address.startswith("https://"):
			raise ConfigError("each cluster peer needs node_id and an HTTPS address")
		peers.append(ClusterPeer(node_id=node_id, address=address))

	cluster = ClusterConfig(
		node_id=text(values, "node_id"),
		password=text(values, "password"),
		previous_password=text(values, "previous_password"),
		previous_password_valid_until=integer(values, "previous_password_valid_until"),
		state_path=Path(text(values, "state_path") or default_state_path),
		peers=tuple(peers),
	)
	if any((cluster.node_id, cluster.password, cluster.peers)) and not cluster.is_enabled:
		raise ConfigError("[cluster] needs node_id, password, and peers")
	if cluster.is_enabled and cluster.node_id not in {peer.node_id for peer in cluster.peers}:
		raise ConfigError("cluster.peers must include cluster.node_id")
	if len({peer.node_id for peer in cluster.peers}) != len(cluster.peers):
		raise ConfigError("cluster.peers contains a duplicate node_id")
	if len(cluster.peers) > MAXIMUM_CLUSTER_MEMBERS:
		raise ConfigError(f"cluster.peers supports at most {MAXIMUM_CLUSTER_MEMBERS} members")
	return cluster


def _validate_jwks_authority(values: dict[str, object], issuers: list[str], audience_service: str) -> None:
	if not issuers:
		return
	if len(issuers) != 2 or issuers.count("central") != 1:
		raise ConfigError("auth.jwks_issuers must contain central and one regional Atlas issuer")

	atlas_issuer = next((issuer for issuer in issuers if issuer != "central"), "")
	match = re.fullmatch(r"atlas:([0-9]+)", atlas_issuer)
	if match is None:
		raise ConfigError("the regional Atlas issuer must use atlas:<region ID>")
	if text(values, "jwks_audience_id") != f"{audience_service}:{match.group(1)}":
		raise ConfigError("auth.jwks_audience_id must match the regional Atlas issuer")


def section(document: dict[str, object], name: str) -> dict[str, object]:
	"""Return one TOML table. A missing table is empty."""
	value = document.get(name, {})
	if not isinstance(value, dict):
		raise ConfigError(f"[{name}] must be a table")
	return value


def text(values: dict[str, object], key: str) -> str:
	"""Return one stripped string value. A missing value is empty."""
	value = values.get(key, "")
	if not isinstance(value, str):
		raise ConfigError(f"{key} must be a string")
	return value.strip()


def integer(values: dict[str, object], key: str) -> int:
	"""Return one integer value. A missing value is 0."""
	value = values.get(key, 0)
	if isinstance(value, bool) or not isinstance(value, int):
		raise ConfigError(f"{key} must be an integer")
	return value
