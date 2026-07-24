"""Backfill `Server.signing_public_key` / `signing_private_key` for legacy hosts.

A Server bootstrapped before the ed25519 signing keypair fields existed
(spec/31 §19.3 / §19.4) has an EMPTY `signing_public_key`. When another host's
`seed.json` is built from the Server table
(`server.py:_write_ancp_bootstrap_state`), that legacy host would seed with an
empty signing pubkey. `seed.signing_pubkey_index` drops empty entries, so peers
have NO cached signing key for it and demand a §19.5 introduction certificate on
first contact — but the introduction cert only rides the host's OWN first direct
MembershipAdvertisement, never a relayed/gossiped record. A peer that first
learns of the legacy host via a RELAYED record then has neither a cached key nor
the cert and silently drops it (`signature_failed`) → a one-sided partition
until a manual `resync_networkd_keys`.

This patch generates a fresh ed25519 signing keypair for every Server missing
one and persists both halves, so `bench migrate` leaves NO Server with an empty
signing key. The controller PUSHES the stored private key to the host on the
next bootstrap / resync (`_write_ancp_bootstrap_state` — the same discipline the
derived wg key uses), and the daemon's idempotent `ensure_signing_keypair`
adopts the pushed files verbatim, so a backfilled key becomes the host's key
with no controller-vs-host divergence (the on-disk read-back canary there guards
the one path that could diverge).

Idempotent: a Server that already has a `signing_public_key` is skipped, so a
re-run — or a fresh install whose rows were seeded with keys at insert — is a
no-op.
"""

import frappe

from atlas.atlas.networking import generate_host_signing_keypair


def execute() -> None:
	# `signing_public_key` is the anchor the seed builder reads; a legacy row has
	# it empty (or NULL). Every such row also lacks the matching private key.
	legacy = frappe.get_all(
		"Server",
		filters={"signing_public_key": ("in", ("", None))},
		pluck="name",
	)
	for name in legacy:
		server = frappe.get_doc("Server", name)
		if server.signing_public_key:
			continue  # populated between the query and here — nothing to do
		priv_b64, pub_b64 = generate_host_signing_keypair()
		server.signing_public_key = pub_b64
		# `signing_private_key` is a Frappe Password field; assigning the plaintext
		# and saving routes it through `_save_passwords`, which encrypts it into
		# `__Auth`. `get_password` on the controller reads it back decrypted.
		server.signing_private_key = priv_b64
		server.save(ignore_permissions=True)
