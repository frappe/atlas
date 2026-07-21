"""Bootstrap seed loader (spec §8 / §9.2 / §19.4 — the trust directory root).

The seed file is the **only** out-of-band input at first boot: an operator-
signed list of currently-known active hosts, installed at provision time into
`/etc/atlas-networkd/seed.json`. atlas-networkd trust-on-first-uses these as
its initial Membership Records, dials each over plain UDP on each seed's
public `endpoint` (port 7946 — see §5, §9.1), and from then on the records
mutate only by signed, higher-generation advertisements from their respective
origins (§19.2 — the cross-origin forwarding ban).

Stage 5+ — the seed now also carries each host's ed25519 `signing_public_key`
so the receiver's `daemon.signing_pubkey_cache` can be pre-populated at build
time (spec §19.4 — the seed-anchored trust directory). The companion
`/etc/atlas-networkd/operator-public-key` carries the operator's provision
pubkey — the trust root for §19.5 newcomer introduction certificates. The
controller writes both files at provision time via
`server.py:_write_ancp_bootstrap_state` and signs the seed file alone with
the operator's provision key (TODO: signature-verify at load time — spec §19.4
says it must; the current loader reads JSON, the signature verify seam is the
controller's responsibility and tracked separately).

Shape (one entry per known host):

    [
      { "host_id": "...", "endpoint": "2001:db9::7",
        "wg_public_key":      "base64...",
        "signing_public_key": "base64...",            # §19.4 — the §19.1/§19.3 trust anchor
        "mesh_address":       "fdaa:0:0:a1b2::1",
        "generation": 1 },
      ...
    ]

Plus the operator's provision pubkey at `/etc/atlas-networkd/operator-public-key`
(base64 ed25519, 0644) — the §19.5 newcomer introduction trust root.
"""

from __future__ import annotations

import json
from pathlib import Path

from .records import MembershipKind, MembershipRecord, MemberState

# The out-of-band operator provision pubkey file. Written by the controller
# alongside seed.json at provision time (`server.py:_write_ancp_bootstrap_state`);
# read by `main.py:build_initial` and stored on the daemon as
# `operator_public_key` for §19.5 newcomer introduction verify.
DEFAULT_OPERATOR_PUBKEY_PATH = "/etc/atlas-networkd/operator-public-key"


def load_seed(path: str) -> list[MembershipRecord]:
	"""Read the seed file and return the initial Membership Records, all marked
	`alive` / `member` at the generation the seed carries, with their
	ed25519 `signing_public_key` populated (spec §19.4). A missing seed file
	raises — a fresh host with no seed cannot join (spec §9.2) and silent
	peer-empty bring-up would mask a misconfigured provision. Use
	`load_seed_optional` if the caller wants a "no seeds yet, come up
	peer-empty and wait" posture (spec §9.2 last paragraph — the newcomer
	retries seeds every `join_retry_interval` until one answers)."""
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
	come-up-peer-empty path or a test harness. A present-but-malformed file
	still raises loud."""
	p = Path(path)
	if not p.exists():
		return []
	return load_seed(path)


def signing_pubkey_index(records: list[MembershipRecord]) -> dict[str, str]:
	"""Build `HostID → signing_public_key` from the loaded seed records — the
	`daemon.signing_pubkey_cache` initial state (spec §19.4). Skips entries
	whose `signing_public_key` is empty (a host bootstrapped before Stage 5+;
	the envelope verifier will demand a §19.5 introduction cert on first
	contact)."""
	return {r.host_id: r.signing_public_key for r in records if r.signing_public_key}


def load_operator_pubkey(path: str = DEFAULT_OPERATOR_PUBKEY_PATH) -> str:
	"""Read the operator's provision pubkey file (a one-line base64 ed25519
	32-byte key, spec §19.4 / §19.5). Returns "" if the file is absent — the
	in-test path or a cluster not yet wired with operator signing. A
	present-but-malformed file raises loud (a bad operator pubkey would mean
	newcomers can't be introduced)."""
	p = Path(path)
	if not p.exists():
		return ""
	body = p.read_text(encoding="utf-8").strip()
	if not body:
		return ""
	# A base64 string of 32 raw bytes is ~44 chars including padding. Don't
	# enforce the exact shape here — let `signing.verify_introduction` raise
	# if the bytes are malformed (one fail-loud path, no duplicate parsing).
	return body


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
			# §19.4 — the seed anchors per-host signing pubkeys for §19.1/§19.3
			# envelope + per-record verify. Empty for hosts bootstrapped
			# before Stage 5+ (the envelope verifier will demand an
			# introduction cert on first contact).
			signing_public_key=entry.get("signing_public_key", ""),
		)
	except KeyError as missing:
		raise ValueError(f"seed entry in {path} missing field {missing}: {entry!r}") from missing


__all__ = [
	"DEFAULT_OPERATOR_PUBKEY_PATH",
	"load_operator_pubkey",
	"load_seed",
	"load_seed_optional",
	"signing_pubkey_index",
]
