"""Load this host's own identity (spec §8 bootstrap contract).

The identity file is the part of the bootstrap contract that is *specific to
this host* (the seed file, by contrast, lists every OTHER known host). It
carries:

    {
      "host_id": "<uuid>",                 # the Frappe Server UUID at provision
      "endpoint": "2001:db9::7",           # this host's public IPv6 (no port)
      "mesh_address": "fdaa:0:0:a1b2::1"   # derived HKDF infra /128 (spec §7.1)
    }

`mesh_address` is *derived* from `host_id` (via `derive_host_mesh_address` in
the controller-side `networking.py`); the bootstrap path pre-computes it at
provision and writes it here so the host doesn't need the controller's HKDF
code on-host. The keypair is separate (Issue A) — `keys.ensure_keypair`.

Pure reader: small typed `HostIdentity` + a `load_identity(path)` that raises
loudly on a missing/malformed file. A fresh host that the operator forgot to
provision the identity file must not come up peer-empty and silently broadcast
gen-1 records claiming the empty identity — that would pollute the cluster.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_IDENTITY_PATH = "/etc/atlas-networkd/identity.json"


@dataclass(frozen=True, slots=True)
class HostIdentity:
	"""This host's own identity — the fields the daemon needs to self-identify
	in Membership Records and to know which pubv6 to advertise as its wg
	endpoint. Mirrors the bootstrap contract (spec §8)."""

	host_id: str
	endpoint: str  # bare public IPv6
	mesh_address: str  # fdaa:0:0:<idx>::1 — the infra /48 bus /128


def load_identity(path: str = _DEFAULT_IDENTITY_PATH) -> HostIdentity:
	"""Read the identity file. Raises `FileNotFoundError` on a missing file
	(provisioning didn't write it — fail loud) and `ValueError` on a malformed
	one (missing fields, wrong shape)."""
	p = Path(path)
	if not p.exists():
		raise FileNotFoundError(f"identity file not found at {path}")
	with p.open("r", encoding="utf-8") as fh:
		data = json.load(fh)
	if not isinstance(data, dict):
		raise ValueError(f"identity file at {path} is not a dict")
	try:
		return HostIdentity(
			host_id=data["host_id"],
			endpoint=data["endpoint"],
			mesh_address=data["mesh_address"],
		)
	except KeyError as missing:
		raise ValueError(f"identity file at {path} missing field {missing}") from missing


__all__ = ["_DEFAULT_IDENTITY_PATH", "HostIdentity", "load_identity"]
