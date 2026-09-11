import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path("/etc/atlas/proxy-control.toml")
DEFAULT_ADMIN_SOCKET = "/run/nginx/admin.sock"
DEFAULT_CERT_DIR = Path("/var/lib/nginx/certs")
LISTEN_ADDRESS = "127.0.0.1"


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
	"""One proxy cluster member."""

	node_id: str
	address: str


@dataclass(frozen=True)
class ClusterConfig:
	"""Regional proxy cluster configuration."""

	node_id: str = ""
	password: str = ""
	previous_password: str = ""
	previous_password_valid_until: int = 0
	state_path: Path = Path("/var/lib/nginx/cluster-state.json")
	peers: tuple[ClusterPeer, ...] = ()

	@property
	def is_enabled(self) -> bool:
		"""Report whether this proxy has cluster membership."""
		return bool(self.node_id and self.password and self.peers)


@dataclass(frozen=True)
class TLSConfig:
	"""Wildcard certificate configuration."""

	wildcard_domain: str
	fullchain_pem: str
	private_key_pem: str


@dataclass(frozen=True)
class ControlConfig:
	"""Control daemon configuration."""

	tls: TLSConfig
	admin_socket: str = DEFAULT_ADMIN_SOCKET
	cert_dir: Path = DEFAULT_CERT_DIR
	domain: str = ""
	node_domain: str = ""
	auth: AuthConfig = AuthConfig()
	auto_proxy_address_prefix: str = ""
	auto_proxy_host_prefixes: tuple[str, ...] = ()
	cluster: ClusterConfig = ClusterConfig()

	@property
	def reserved_subdomains(self) -> tuple[str, ...]:
		"""Return the control subdomains."""
		return tuple(domain.partition(".")[0] for domain in (self.domain, self.node_domain) if domain)

	@property
	def reserved_subdomain(self) -> str:
		"""Return the primary control subdomain."""
		return self.reserved_subdomains[0] if self.reserved_subdomains else ""


def config_path() -> Path:
	"""Return the configuration path."""
	return Path(os.environ.get("ATLAS_PROXY_CONTROL_CONFIG") or CONFIG_PATH)


def load(path: Path | None = None) -> ControlConfig:
	"""Load the control configuration."""
	document = _read(path or config_path())
	control = _section(document, "control")
	auth = _section(document, "auth")
	jwks_issuers = auth.get("jwks_issuers", [])
	if not isinstance(jwks_issuers, list) or not all(isinstance(value, str) for value in jwks_issuers):
		raise ConfigError("auth.jwks_issuers must be an array of strings")
	_validate_jwks_authority(auth, jwks_issuers)
	auto_proxy = _section(document, "auto_proxy")
	host_prefixes = auto_proxy.get("host_prefixes", [])
	if not isinstance(host_prefixes, list) or not all(isinstance(value, str) for value in host_prefixes):
		raise ConfigError("auto_proxy.host_prefixes must be an array of strings")
	if "port" in control:
		raise ConfigError("control.port is fixed at 9000")

	tls = _tls(_section(document, "tls"))
	return ControlConfig(
		admin_socket=_text(control, "admin_socket") or DEFAULT_ADMIN_SOCKET,
		cert_dir=Path(_text(control, "cert_dir") or DEFAULT_CERT_DIR),
		domain=_domain(_text(control, "domain"), tls),
		node_domain=_domain(_text(control, "node_domain"), tls),
		auth=AuthConfig(
			password_hash=_text(auth, "password_hash"),
			previous_password_hash=_text(auth, "previous_password_hash"),
			previous_password_valid_until=_integer(auth, "previous_password_valid_until"),
			jwks_url=_text(auth, "jwks_url"),
			jwks_audience_id=_text(auth, "jwks_audience_id"),
			jwks_issuers=tuple(jwks_issuers),
		),
		auto_proxy_address_prefix=_text(auto_proxy, "address_prefix"),
		auto_proxy_host_prefixes=tuple(host_prefixes),
		cluster=_cluster(_section(document, "cluster")),
		tls=tls,
	)


def _cluster(section: dict[str, object]) -> ClusterConfig:
	"""Return the proxy cluster configuration."""
	raw_peers = section.get("peers", [])
	if not isinstance(raw_peers, list):
		raise ConfigError("cluster.peers must be an array")

	peers = []
	for raw_peer in raw_peers:
		if not isinstance(raw_peer, dict):
			raise ConfigError("each cluster peer must be a table")
		node_id = _text(raw_peer, "node_id")
		address = _text(raw_peer, "address").rstrip("/")
		if not node_id or not address.startswith("https://"):
			raise ConfigError("each cluster peer needs node_id and an HTTPS address")
		peers.append(ClusterPeer(node_id=node_id, address=address))

	cluster = ClusterConfig(
		node_id=_text(section, "node_id"),
		password=_text(section, "password"),
		previous_password=_text(section, "previous_password"),
		previous_password_valid_until=_integer(section, "previous_password_valid_until"),
		state_path=Path(_text(section, "state_path") or "/var/lib/nginx/cluster-state.json"),
		peers=tuple(peers),
	)
	if any((cluster.node_id, cluster.password, cluster.peers)) and not cluster.is_enabled:
		raise ConfigError("[cluster] needs node_id, password, and peers")
	if cluster.is_enabled and cluster.node_id not in {peer.node_id for peer in cluster.peers}:
		raise ConfigError("cluster.peers must include cluster.node_id")
	if len({peer.node_id for peer in cluster.peers}) != len(cluster.peers):
		raise ConfigError("cluster.peers contains a duplicate node_id")
	if len(cluster.peers) > 5:
		raise ConfigError("cluster.peers supports at most 5 members")
	return cluster


def _validate_jwks_authority(section: dict[str, object], issuers: list[str]) -> None:
	if not issuers:
		return
	if len(issuers) != 2 or issuers.count("central") != 1:
		raise ConfigError("auth.jwks_issuers must contain central and one regional Atlas issuer")

	atlas_issuer = next((issuer for issuer in issuers if issuer != "central"), "")
	match = re.fullmatch(r"atlas:([0-9]+)", atlas_issuer)
	if match is None:
		raise ConfigError("the regional Atlas issuer must use atlas:<region ID>")
	if _text(section, "jwks_audience_id") != f"atlas-proxy:{match.group(1)}":
		raise ConfigError("auth.jwks_audience_id must match the regional Atlas issuer")


def _domain(domain: str, tls: TLSConfig) -> str:
	"""Validate the control domain."""
	if not domain:
		return ""

	domain = domain.lower()
	zone = tls.wildcard_domain.lower().removeprefix("*.")
	label, separator, rest = domain.partition(".")
	if not label or not separator or rest != zone:
		raise ConfigError(f"control.domain {domain} must be one label below {zone}")

	return domain


def _read(path: Path) -> dict[str, object]:
	try:
		with path.open("rb") as source:
			return tomllib.load(source)
	except FileNotFoundError:
		return {}
	except OSError as error:
		raise ConfigError(f"cannot read {path}: {error}") from error
	except tomllib.TOMLDecodeError as error:
		raise ConfigError(f"{path} is not valid TOML: {error}") from error


def _tls(section: dict[str, object]) -> TLSConfig:
	"""Return the required wildcard certificate."""
	if "enabled" in section:
		raise ConfigError("tls.enabled is not supported")

	wildcard_domain = _text(section, "wildcard_domain")
	fullchain_pem = _text(section, "fullchain_pem")
	private_key_pem = _text(section, "private_key_pem")
	if not (wildcard_domain and fullchain_pem and private_key_pem):
		raise ConfigError("[tls] needs wildcard_domain, fullchain_pem, and private_key_pem")

	return TLSConfig(wildcard_domain, fullchain_pem, private_key_pem)


def _section(document: dict[str, object], name: str) -> dict[str, object]:
	value = document.get(name, {})
	if not isinstance(value, dict):
		raise ConfigError(f"[{name}] must be a table")
	return value


def _text(section: dict[str, object], key: str) -> str:
	value = section.get(key, "")
	if not isinstance(value, str):
		raise ConfigError(f"{key} must be a string")
	return value.strip()


def _integer(section: dict[str, object], key: str) -> int:
	value = section.get(key, 0)
	if isinstance(value, bool) or not isinstance(value, int):
		raise ConfigError(f"{key} must be an integer")
	return value
