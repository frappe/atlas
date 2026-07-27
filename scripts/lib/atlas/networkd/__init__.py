"""atlas-networkd — the decentralized network control plane daemon (spec/31).

Long-running successor to `host-mesh.service` + the controller-driven
`atlas/atlas/host_mesh.py` reconcile. Every compute host runs one
`atlas-networkd`; together they maintain two replicated datasets — Membership
("how to reach a host") and Ownership ("which host owns a /128") — over gossip
(spec §13) and anti-entropy (spec §15), and program `wg-mesh` atomically from
the effective tables (spec §16).

This package is split so the **pure** pieces (record types, the effective-table
computation, the WgDesired render, the apply command builders, persistence, the
config, the local-ownership reader) are unit-testable with bare `python3 -m
unittest` — no host, no Frappe, no daemon loop. The loop + transport land in
later stages (see TODO list); this stage 1a ships only the offline-verifiable
substrate everything else is built on, exactly mirroring how `host_mesh.py`
split its pure command builders from the host-touching `bring_up_mesh`.

Convention: every module here is stdlib-only (the host package declares no
dependencies; spec/03). If a real dep is needed later (e.g. `cryptography` for
the §19.3 ed25519 signatures), add it to `scripts/host-pyproject.toml` and
mirror in `scripts/pyproject.toml` — do not silently import.
"""
