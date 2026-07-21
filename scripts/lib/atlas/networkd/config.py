"""`atlas-networkd` config (spec §14.3 + the §11/§12/§13/§16 knobs).

`Config` is one typed dataclass + a `load(path)` that reads `/etc/atlas-networkd/
ancp.toml` via stdlib `tomllib` and overlays the user's values on the spec
defaults. Operators tune suspicion/gossip/anti-entropy/MTU here; everything the
§14.3 timer table lists is a field, plus the paths + apply knobs.

Pure: no host touch, no I/O except the (testable) TOML read. A missing file
returns the defaults (a fresh host with no `ancp.toml` runs with spec defaults).
A bad TOML raises loudly — fail at the boundary (Taste.md), do not fall back.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

# --- spec §14.3 defaults ------------------------------------------------------
# Failure-detection ladder
DEFAULT_PROBE_INTERVAL = 1.0  # seconds (floats so fractional ms work)
DEFAULT_PROBE_TIMEOUT = 0.5
DEFAULT_INDIRECT_TIMEOUT = 2.0
DEFAULT_PROBE_PEERS = 3
DEFAULT_INDIRECT_RELAYS = 3
DEFAULT_SUSPECT_TIMEOUT = 10.0  # the partition knob (§14.3) — raise for WAN
DEFAULT_DEAD_GRACE = 30.0
DEFAULT_OWNERSHIP_GRACE = 60.0  # strictly > suspect_timeout + dead_grace
DEFAULT_LEAVING_GRACE = 2.0
DEFAULT_HEARTBEAT_INTERVAL = 1.0  # piggybacks on gossip rounds (§14.2)

# Gossip / anti-entropy (§13 / §15)
DEFAULT_GOSSIP_INTERVAL = 0.2
DEFAULT_GOSSIP_FANOUT = 3
DEFAULT_GOSSIP_FORWARD_BUDGET = 16
DEFAULT_ANTI_ENTROPY_INTERVAL = 1.0
DEFAULT_ANTI_ENTROPY_MERKLE_THRESHOLD = 100  # hosts; below this, naive pull
DEFAULT_SEEN_CACHE_SIZE = 10_000

# Ownership scan / advertisement (§11 / §12)
DEFAULT_OWNERSHIP_SCAN_INTERVAL = 2.0
DEFAULT_ADVERTISEMENT_REFRESH_INTERVAL = 60.0

# Apply pipeline (§16.4)
DEFAULT_APPLY_DEBOUNCE = 0.2

# WireGuard constants (mirrors `scripts/lib/atlas/host_mesh.py`)
DEFAULT_WG_HOST_PORT = 51820
DEFAULT_WIREGUARD_MTU = 1420
DEFAULT_WG_DEVICE = "wg-mesh"
PRIVATE_PLANE_ROUTE = "fdaa::/16"
# The UDP port ANCP listens on INSIDE wg-mesh (spec §13). The wg-listener port
# (51820) is WireGuard's own; ANCP rides UDP one layer above, dialed by a peer's
# mesh_address (an fdaa:: /128). Default 7946 (the same port Serf/memberlist
# use; coincidental, but well-known to operators).
DEFAULT_ANCP_PORT = 7946

# Path layout (Issue A — keys live under the daemon's data dir, not the
# controller-pushed `/etc/atlas-host-mesh.{env,key}` files of the predecessor).
DEFAULT_DATA_DIR = "/var/lib/atlas-networkd"
DEFAULT_CONFIG_DIR = "/etc/atlas-networkd"
DEFAULT_SEED_PATH = f"{DEFAULT_CONFIG_DIR}/seed.json"
DEFAULT_LOCAL_OWNERSHIP_PATH = f"{DEFAULT_CONFIG_DIR}/local-ownership.json"
DEFAULT_PRIVATE_KEY_PATH = f"{DEFAULT_CONFIG_DIR}/wg-private-key"
DEFAULT_PUBLIC_KEY_PATH = f"{DEFAULT_CONFIG_DIR}/wg-public-key"
DEFAULT_IDENTITY_PATH = f"{DEFAULT_CONFIG_DIR}/identity.json"
DEFAULT_TOML_PATH = f"{DEFAULT_CONFIG_DIR}/ancp.toml"


@dataclass(frozen=True, slots=True)
class Config:
	"""The full tunable surface for `atlas-networkd`. All §14.3 timers, the §11/
	§12 cadences, §13 gossip fan-out, §15 anti-entropy, §16 apply debounce, and
	the path layout. `load()` overlays a TOML file on these defaults.
	"""

	# §14.3 timers
	probe_interval: float = DEFAULT_PROBE_INTERVAL
	probe_timeout: float = DEFAULT_PROBE_TIMEOUT
	indirect_timeout: float = DEFAULT_INDIRECT_TIMEOUT
	probe_peers: int = DEFAULT_PROBE_PEERS
	indirect_relays: int = DEFAULT_INDIRECT_RELAYS
	suspect_timeout: float = DEFAULT_SUSPECT_TIMEOUT
	dead_grace: float = DEFAULT_DEAD_GRACE
	ownership_grace: float = DEFAULT_OWNERSHIP_GRACE
	leaving_grace: float = DEFAULT_LEAVING_GRACE
	heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
	# §13 gossip
	gossip_interval: float = DEFAULT_GOSSIP_INTERVAL
	gossip_fanout: int = DEFAULT_GOSSIP_FANOUT
	gossip_forward_budget: int = DEFAULT_GOSSIP_FORWARD_BUDGET
	seen_cache_size: int = DEFAULT_SEEN_CACHE_SIZE
	# §15 anti-entropy
	anti_entropy_interval: float = DEFAULT_ANTI_ENTROPY_INTERVAL
	anti_entropy_merkle_threshold: int = DEFAULT_ANTI_ENTROPY_MERKLE_THRESHOLD
	# §11 / §12
	ownership_scan_interval: float = DEFAULT_OWNERSHIP_SCAN_INTERVAL
	advertisement_refresh_interval: float = DEFAULT_ADVERTISEMENT_REFRESH_INTERVAL
	# §16.4
	apply_debounce: float = DEFAULT_APPLY_DEBOUNCE
	# WireGuard + paths
	wg_host_port: int = DEFAULT_WG_HOST_PORT
	wireguard_mtu: int = DEFAULT_WIREGUARD_MTU
	wg_device: str = DEFAULT_WG_DEVICE
	ancp_port: int = DEFAULT_ANCP_PORT  # the plain-UDP ANCP port on the public IPv6 endpoint (§5, §13)
	data_dir: str = DEFAULT_DATA_DIR
	config_dir: str = DEFAULT_CONFIG_DIR
	seed_path: str = DEFAULT_SEED_PATH
	local_ownership_path: str = DEFAULT_LOCAL_OWNERSHIP_PATH
	private_key_path: str = DEFAULT_PRIVATE_KEY_PATH
	public_key_path: str = DEFAULT_PUBLIC_KEY_PATH
	identity_path: str = DEFAULT_IDENTITY_PATH

	def with_overrides(self, **overrides) -> "Config":
		"""Return a new `Config` with the given fields overridden. A bad key raises
		`TypeError` (fail loud at the boundary) — protects against a typo silent
		no-op in the daemon's bootstrap path. Uses `dataclasses.replace` because
		`slots=True` Config has no `__dict__` to splat."""
		valid = {f.name for f in fields(Config)}
		bad = set(overrides) - valid
		if bad:
			raise TypeError(f"unknown Config field(s): {sorted(bad)}")
		return replace(self, **overrides)


def load(path: str | Path = DEFAULT_TOML_PATH) -> Config:
	"""Read `ancp.toml` and return a `Config` with the user's values overlaid on
	the spec defaults (§14.3 et al.). A missing file returns the defaults — a
	fresh host with no `ancp.toml` runs with spec defaults, the same posture
	`host-mesh.service` takes toward its env file. An unreadable or bad TOML
	raises; do not fall back (Taste.md)."""
	p = Path(path)
	if not p.exists():
		return Config()
	with p.open("rb") as fh:
		data = tomllib.load(fh)
	# Unknown keys raise (fail loud); a typo'd knob should surface, not silently
	# no-op. `with_overrides` enforces the field-name check.
	return Config().with_overrides(**_coerce_overrides(data))


def _coerce_overrides(data: dict) -> dict:
	"""Drop top-level `[ancp]` / `[xyz]` table wrappers if present, so the file
	can be either flat or tabled. We accept a single flat document (every knob at
	the top level) for ergonomics; a wrapped `[ancp]` table also works. Tables
	other than `[ancp]` are rejected to keep the surface minimal."""
	if not data:
		return {}
	# Accept a top-level [ancp] table; reject any other table to surface typos.
	table_keys = [k for k, v in data.items() if isinstance(v, dict)]
	if table_keys:
		if table_keys != ["ancp"]:
			raise ValueError(f"unknown TOML table(s): {table_keys}")
		return data["ancp"]
	return data


__all__ = [
	"DEFAULT_ADVERTISEMENT_REFRESH_INTERVAL",
	"DEFAULT_ANCP_PORT",
	"DEFAULT_ANTI_ENTROPY_INTERVAL",
	"DEFAULT_ANTI_ENTROPY_MERKLE_THRESHOLD",
	"DEFAULT_APPLY_DEBOUNCE",
	"DEFAULT_CONFIG_DIR",
	"DEFAULT_DATA_DIR",
	"DEFAULT_DEAD_GRACE",
	"DEFAULT_GOSSIP_FANOUT",
	"DEFAULT_GOSSIP_FORWARD_BUDGET",
	"DEFAULT_GOSSIP_INTERVAL",
	"DEFAULT_HEARTBEAT_INTERVAL",
	"DEFAULT_IDENTITY_PATH",
	"DEFAULT_INDIRECT_RELAYS",
	"DEFAULT_INDIRECT_TIMEOUT",
	"DEFAULT_LEAVING_GRACE",
	"DEFAULT_LOCAL_OWNERSHIP_PATH",
	"DEFAULT_OWNERSHIP_GRACE",
	"DEFAULT_OWNERSHIP_SCAN_INTERVAL",
	"DEFAULT_PRIVATE_KEY_PATH",
	"DEFAULT_PROBE_INTERVAL",
	"DEFAULT_PROBE_PEERS",
	"DEFAULT_PROBE_TIMEOUT",
	"DEFAULT_PUBLIC_KEY_PATH",
	"DEFAULT_SEED_PATH",
	"DEFAULT_SEEN_CACHE_SIZE",
	"DEFAULT_SUSPECT_TIMEOUT",
	"DEFAULT_TOML_PATH",
	"DEFAULT_WG_DEVICE",
	"DEFAULT_WG_HOST_PORT",
	"DEFAULT_WIREGUARD_MTU",
	"PRIVATE_PLANE_ROUTE",
	"Config",
	"load",
]
