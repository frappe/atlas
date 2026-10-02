import os
from dataclasses import dataclass
from pathlib import Path

from atlas_control.config import (
	AuthConfig,
	ClusterConfig,
	ConfigError,
	auth_loader,
	parse_auth,
	parse_cluster,
	read_toml,
	section,
	text,
)

CONFIG_PATH = Path("/etc/atlas/proxy-control.toml")
DEFAULT_ADMIN_SOCKET = "/run/nginx/admin.sock"
DEFAULT_CERT_DIR = Path("/var/lib/nginx/certs")
LISTEN_ADDRESS = "127.0.0.1"
AUDIENCE_SERVICE = "atlas-proxy"
CLUSTER_STATE_PATH = Path("/var/lib/nginx/cluster-state.json")


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
	cluster: ClusterConfig = ClusterConfig(state_path=CLUSTER_STATE_PATH)

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


def load_auth(path: Path | None = None) -> AuthConfig:
	"""Return the current caller credentials. An invalid file grants nothing."""
	return auth_loader(path or config_path(), AUDIENCE_SERVICE)()


def load(path: Path | None = None) -> ControlConfig:
	"""Load the control configuration."""
	document = read_toml(path or config_path())
	control = section(document, "control")
	auto_proxy = section(document, "auto_proxy")
	host_prefixes = auto_proxy.get("host_prefixes", [])
	if not isinstance(host_prefixes, list) or not all(isinstance(value, str) for value in host_prefixes):
		raise ConfigError("auto_proxy.host_prefixes must be an array of strings")
	if "port" in control:
		raise ConfigError("control.port is fixed at 9000")

	tls = _tls(section(document, "tls"))
	return ControlConfig(
		admin_socket=text(control, "admin_socket") or DEFAULT_ADMIN_SOCKET,
		cert_dir=Path(text(control, "cert_dir") or DEFAULT_CERT_DIR),
		domain=_domain(text(control, "domain"), tls),
		node_domain=_domain(text(control, "node_domain"), tls),
		auth=parse_auth(section(document, "auth"), AUDIENCE_SERVICE),
		auto_proxy_address_prefix=text(auto_proxy, "address_prefix"),
		auto_proxy_host_prefixes=tuple(host_prefixes),
		cluster=parse_cluster(section(document, "cluster"), CLUSTER_STATE_PATH),
		tls=tls,
	)


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


def _tls(values: dict[str, object]) -> TLSConfig:
	"""Return the required wildcard certificate."""
	if "enabled" in values:
		raise ConfigError("tls.enabled is not supported")

	wildcard_domain = text(values, "wildcard_domain")
	fullchain_pem = text(values, "fullchain_pem")
	private_key_pem = text(values, "private_key_pem")
	if not (wildcard_domain and fullchain_pem and private_key_pem):
		raise ConfigError("[tls] needs wildcard_domain, fullchain_pem, and private_key_pem")

	return TLSConfig(wildcard_domain, fullchain_pem, private_key_pem)
