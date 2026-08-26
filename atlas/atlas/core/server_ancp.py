"""Pure builders for a host's ANCP seed-of-trust — the bytes Atlas computes
before the bootstrap Task starts `atlas-networkd.service` (spec/31 §7.1, §8,
§9.2, §19.3-19.5): this host's identity, the seed of every other Active host,
the wg-mesh keypair, and the operator ed25519 signatures over the seed and this
host's introduction.

Extracted from the `Server` controller: assembling and signing the seed is one
cohesive, security-critical responsibility, separable from the host SSH writes
that push the files. Everything here is PURE — it reads the doc and Atlas
Settings and derives/serialises/signs, but touches no host. The `run_ssh`
writes (and the read-back canary) stay on the controller as
`_push_host_key_files` / `_push_seed_files`, so the `run_ssh` test mock seam is
preserved on `server.py` exactly as `server_boat.py` keeps `_boat_ssh` there.

Free functions taking the `Server`, following the server_boat.py /
vm_provisioning.py pattern. The networking / atlas_settings imports are deferred
inside each function, matching the controller's original (it avoids an import
cycle at module load).

SECURITY INVARIANTS preserved here verbatim — do not "clean up":
- `seed_document` is `json.dumps(seed, sort_keys=True) + "\n"`, byte-for-byte;
  the operator signature is computed over these EXACT bytes and the host's
  `seed.load_seed` verifies against them (spec §9.2 / §19.4 — the seed is the
  sole trust root, fail-closed).
- `build_seed` SKIPS (never emits an empty entry for) an Active peer with an
  empty `signing_public_key`, warning loud — an emitted-empty entry one-sidedly
  partitions the peer (§19.4). The `backfill_server_signing_key` migration is
  the real fix; this is the belt-and-braces guard.
- the signing module is loaded once and reused for BOTH the seed signature and
  the introduction signature.
"""

from __future__ import annotations

import json

import frappe


def build_identity(server) -> dict:
	"""This host's `(host_id, endpoint, mesh_address)` — written to identity.json."""
	from atlas.atlas.core.networking import derive_host_mesh_address

	return {
		"host_id": server.name,
		"endpoint": server.ipv6_address,
		"mesh_address": server.mesh_address or derive_host_mesh_address(server.name),
	}


def build_seed(server) -> list[dict]:
	"""The seed = every OTHER Active Server (excluding this one). The daemon
	will reconcile any drift via gossip+anti-entropy once it cold-joins."""
	from atlas.atlas.core.networking import derive_host_mesh_address

	other_actives = frappe.get_all(
		"Server",
		filters={"status": "Active", "name": ["!=", server.name]},
		fields=["name", "ipv6_address", "wireguard_public_key", "mesh_address", "signing_public_key"],
	)
	seed = []
	for row in other_actives:
		if not row.ipv6_address:
			continue
		# §19.4 — the seed anchors each other host's ed25519 pubkey so the
		# envelope verifier's `signing_pubkey_cache` is populated at build
		# time. A legacy host bootstrapped before `signing_public_key`
		# existed has an EMPTY key here. SKIP it rather than emit an empty
		# entry: `seed.signing_pubkey_index` drops empty keys anyway, so an
		# emitted-empty entry gives the peer NO cached key AND forces the
		# §19.5 introduction path — but the introduction cert only rides the
		# legacy host's OWN first direct MembershipAdvertisement, never a
		# relayed/gossiped record, so a peer that first learns of it via a
		# relay silently drops (`signature_failed`) → one-sided partition.
		# Skipping is strictly safer: the peer just isn't seeded with this
		# host and learns it later once it HAS a key (the `backfill_server_
		# signing_key` migration fills every legacy row, so this is a
		# belt-and-braces guard for a row that slipped through). Warn loud so
		# an operator sees the gap instead of it silently partitioning.
		signing_public_key = getattr(row, "signing_public_key", "") or ""
		if not signing_public_key:
			frappe.logger("atlas").warning(
				f"skipping {row.name} from {server.name}'s ANCP seed: it has no "
				"signing_public_key (a host bootstrapped before the field existed). "
				"Run `bench migrate` (the backfill_server_signing_key patch) or "
				"resync_networkd_keys on it so it gets a signing key and can be seeded."
			)
			continue
		seed.append(
			{
				"host_id": row.name,
				"endpoint": row.ipv6_address,
				"wg_public_key": row.wireguard_public_key or "",
				"signing_public_key": signing_public_key,
				"mesh_address": row.mesh_address or derive_host_mesh_address(row.name),
				"generation": 1,
			}
		)
	return seed


def seed_document(seed) -> str:
	"""The EXACT bytes pushed to /etc/atlas-networkd/seed.json — sign these so
	the controller's signature is byte-identical to what the host's
	`seed.load_seed` verifies (spec §9.2 / §19.4). `sort_keys=True` + a trailing
	newline is the contract; do not change it."""
	return json.dumps(seed, sort_keys=True) + "\n"


def derive_wireguard_keypair(server) -> tuple[str, str]:
	"""This host's wg-mesh keypair, derived from the Server UUID (spec/31 §7.1).
	Returns `(private, public)`."""
	from atlas.atlas.core.networking import derive_host_wireguard_keypair
	from atlas.atlas.doctype.atlas_settings.atlas_settings import get_ancp_wg_derivation_secret

	return derive_host_wireguard_keypair(server.name, get_ancp_wg_derivation_secret())


def load_host_signing_module():
	"""Load the host-lib's pure ed25519 signing primitives (pure above the
	keypair file — runs in the bench venv where `cryptography` is already a dep).
	Use importlib to bypass the cached top-level `atlas` package (the bench app):
	sys.path insertion alone won't reach scripts/lib/atlas/networkd/signing.py.
	Load once and reuse for the seed signature AND the introduction signature."""
	import importlib.util
	from pathlib import Path

	signing_path = str(
		Path(frappe.get_app_path("atlas")).parent
		/ "scripts"
		/ "lib"
		/ "atlas"
		/ "networkd"
		/ "signing.py"
	)
	spec = importlib.util.spec_from_file_location("_host_signing", signing_path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)  # type: ignore[union-attr]
	return module


def sign_seed_document(seed_content: str, operator_priv: str, *, host_signing) -> str:
	"""The detached operator signature over the EXACT seed.json bytes (spec §9.2
	/ §19.4: the seed is the sole trust root, so the host's `seed.load_seed`
	fails closed unless these bytes verify against operator-public-key)."""
	return host_signing.sign_detached(seed_content.encode("utf-8"), operator_priv)


def build_introduction_signature(server, operator_priv: str, *, host_signing) -> str:
	"""The operator-signed `{host_id, signing_public_key, generation=1}` binding
	for a host joining an EXISTING cluster (§19.5). The verifier accepts the
	self-asserted signing_public_key iff this signature verifies against
	operator-public-key. Initial-seed hosts skip this (their pubkey is already
	anchored on every peer via the seed)."""
	intro_body = {
		"host_id": server.name,
		"signing_public_key": server.signing_public_key,
		"generation": 1,
	}
	return host_signing.sign_introduction(intro_body, operator_priv)
