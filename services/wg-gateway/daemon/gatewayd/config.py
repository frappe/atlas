import os
from dataclasses import dataclass
from pathlib import Path

from atlas_control.config import (
	AuthConfig,
	ClusterConfig,
	ConfigError,
	auth_loader,
	integer,
	parse_auth,
	parse_cluster,
	read_toml,
	section,
	text,
)

CONFIG_PATH = Path("/etc/atlas/wireguard-gateway.toml")
STATE_DIRECTORY = Path("/opt/atlas/wg-gateway")
AUDIENCE_SERVICE = "atlas-wg-gateway"


@dataclass(frozen=True)
class Node:
	"""One gateway node that customer devices connect to."""

	node_id: str
	gateway_id: int
	endpoint: str
	listen_port: int
	public_key: str


@dataclass(frozen=True)
class GatewayConfig:
	"""The configuration that Atlas writes for one gateway node."""

	region_id: int
	node_id: str
	private_key: str
	nodes: tuple[Node, ...]
	auth: AuthConfig
	cluster: ClusterConfig
	certificate_pem: str
	private_key_pem: str
	state_directory: Path = STATE_DIRECTORY

	@property
	def node(self) -> Node:
		"""Return this node."""
		return next(node for node in self.nodes if node.node_id == self.node_id)


def config_path() -> Path:
	"""Return the configuration path."""
	return Path(os.environ.get("ATLAS_WG_GATEWAY_CONFIG") or CONFIG_PATH)


def load_auth() -> AuthConfig:
	"""Return the current caller credentials. An invalid file grants nothing."""
	return auth_loader(config_path(), AUDIENCE_SERVICE)()


def load(path: Path | None = None) -> GatewayConfig:
	"""Load the node configuration and refuse a missing or partial one."""
	document = read_toml(path or config_path())
	gateway = section(document, "gateway")
	tls = section(document, "tls")
	config = GatewayConfig(
		region_id=integer(gateway, "region_id"),
		node_id=text(gateway, "node_id"),
		private_key=text(gateway, "private_key"),
		nodes=_nodes(document.get("nodes", [])),
		auth=parse_auth(section(document, "auth"), AUDIENCE_SERVICE),
		cluster=parse_cluster(section(document, "cluster"), STATE_DIRECTORY / "cluster-state.json"),
		certificate_pem=text(tls, "fullchain_pem"),
		private_key_pem=text(tls, "private_key_pem"),
	)
	if not 0 < config.region_id <= 0xFFFF:
		raise ConfigError("gateway.region_id must be a 16-bit value above 0")
	if not config.private_key or not config.certificate_pem or not config.private_key_pem:
		raise ConfigError("the gateway private key and the [tls] certificate are required")
	if config.node_id not in {node.node_id for node in config.nodes}:
		raise ConfigError("[[nodes]] must include gateway.node_id")
	if not config.cluster.is_enabled or config.cluster.node_id != config.node_id:
		raise ConfigError("[cluster] must list this node, even when it is the only one")
	return config


def _nodes(raw_nodes: object) -> tuple[Node, ...]:
	if not isinstance(raw_nodes, list) or not raw_nodes:
		raise ConfigError("[[nodes]] must list every gateway node")

	nodes = []
	for raw_node in raw_nodes:
		if not isinstance(raw_node, dict):
			raise ConfigError("each gateway node must be a table")
		node = Node(
			node_id=text(raw_node, "node_id"),
			gateway_id=integer(raw_node, "gateway_id"),
			endpoint=text(raw_node, "endpoint"),
			listen_port=integer(raw_node, "listen_port"),
			public_key=text(raw_node, "public_key"),
		)
		if not (node.node_id and node.endpoint and node.public_key):
			raise ConfigError("each gateway node needs node_id, endpoint, and public_key")
		if not (0 < node.gateway_id <= 0xFFFF and 0 < node.listen_port <= 0xFFFF):
			raise ConfigError("gateway_id and listen_port must be 16-bit values above 0")
		nodes.append(node)
	if len({node.node_id for node in nodes}) != len(nodes):
		raise ConfigError("[[nodes]] contains a duplicate node_id")
	return tuple(nodes)
