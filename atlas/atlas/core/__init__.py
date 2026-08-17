"""`core` — the cluster manager: VM lifecycle, migration, snapshotting, image
promotion, and the substrate that runs VMs on hosts (the Boat seam, placement,
servers, providers, the SSH/exec primitive, and the core network plumbing —
per-VM netns/veth/tap, public v6 identity, NAT44, Reserved-IP, the private
mesh/ANCP, the management tunnel, the per-VM firewall).

`core` NEVER imports `services`. It is PaaS-blind: no site/pilot/proxy concept
appears in a core controller. When a VM finishes a lifecycle step, core invokes
registered callbacks BY NAME (see `core.callbacks`) — it does not know what they
do. `services` registers its deploy/health callbacks there.

The one-way rule (`core` ⇏ `services`) is enforced by the readability lint gate
(.github/scripts/lint_gate.py `core_services_imports`).
"""
