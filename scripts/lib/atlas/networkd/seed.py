"""Bootstrap seed loader (spec §8 / §9.2 — Issue A closeout).

The seed file is the **only** out-of-band input at first boot: an operator-
signed list of currently-known active hosts, installed at provision time into
`/etc/atlas-networkd/seed.json`. atlas-networkd trust-on-first-uses these as
its initial Membership Records, dials each over wg-mesh, and from then on the
records mutate only by signed, higher-generation advertisements from their
respective origins (§19.2 — the cross-origin forwarding ban).

Shape (one entry per known host):

    [
      { "host_id": "...", "endpoint": "2001:db9::7",
        "wg_public_key": "base64...", "mesh_address": "fdaa:0:0:a1b2::1",
        "generation": 1 },
      ...
    ]

Signature verification (§8: operator-signed with the provision key) is a stage-5
concern (§19.3 ed25519). Stage 1a ships the unsigned loader: trust-on-first-
use of the file's contents is the seam; the signing layer wraps `load_seed`
later without changing its return type.
"""

from __future__ import annotations

import json
from pathlib import Path

from .records import MembershipKind, MembershipRecord, MemberState


def load_seed(path: str) -> list[MembershipRecord]:
	"""Read the seed file and return the initial Membership Records, all marked
	`alive` / `member` at the generation the seed carries. A missing seed file
	raises — a fresh host with no seed cannot join (spec §9.2) and silent
	peer-empty bring-up would mask a misconfigured provision. Use `load_seed_optional`
	if the caller wants a "no seeds yet, come up peer-empty and wait" posture
	(spec §9.2 last paragraph — the newcomer retries seeds every
	`join_retry_interval` until one answers)."""
	p = Path(path)
	if not p.exists():
		raise FileNotFoundError(f"seed file not found at {path}")
	with p.open("r", encoding="utf-8") as fh:
		data = json.load(fh)
	if not isinstance(data, list):
		raise ValueError(f"seed file at {path} is not a list of host entries")
	return [_seed_entry_to_record(entry, path) for entry in data]


def load_seed_optional(path: str) -> list[MembershipRecord]:
	"""Like `load_seed` but returns `[]` when the file is absent — for the cold
	come-up-peer-empty path or a test harness. A present-but-malformed file still
	raise loud."""
	p = Path(path)
	if not p.exists():
		return []
	return load_seed(path)


def _seed_entry_to_record(entry: dict, path: str) -> MembershipRecord:
	if not isinstance(entry, dict):
		raise ValueError(f"seed entry in {path} is not a dict: {entry!r}")
	try:
		return MembershipRecord(
			host_id=entry["host_id"],
			kind=MembershipKind(entry.get("kind", "member")),
			state=MemberState(entry.get("state", "alive")),
			endpoint=entry["endpoint"],
			wg_public_key=entry.get("wg_public_key", ""),
			mesh_address=entry["mesh_address"],
			generation=int(entry.get("generation", 1)),
		)
	except KeyError as missing:
		raise ValueError(f"seed entry in {path} missing field {missing}: {entry!r}") from missing


__all__ = ["load_seed", "load_seed_optional"]
