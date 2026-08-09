# VM migration — sample implementation

Illustrative, **not committed app code**. These files show *how* the design in
[spec/24-vm-migration.md](../../24-vm-migration.md) would be built, in the exact
idioms of the real codebase (typed `--kebab-case` Task scripts with an
`ATLAS_RESULT=` line, the `ThinPool`/`Identity` libraries, the `flags.<x>` gate
pattern, the scheduler-driven resumable loop). They are written to be *read*, not
run — a few seams are elided (marked in comments) where the full plumbing would
just duplicate `provision-vm.py`.

| File | Maps to | What it shows |
| --- | --- | --- |
| `virtual_machine_migration.json` | `atlas/atlas/doctype/virtual_machine_migration/` | The new doctype: the resumable phase row (status machine, addresses, NBD handle, hydration %, audit). |
| `virtual_machine_migration.py` | same | The doctype controller: immutability guard, `active_migration_for()` (the single-flight + lifecycle guard helper), `retry()`. |
| `virtual_machine_migrate_patch.py` | `atlas/atlas/doctype/virtual_machine/virtual_machine.py` | The three focused edits to the VM controller: the `flags.migrating` gate in `validate()`, the `migrate()` entry point, the `_guard_no_active_migration()` lifecycle guard. |
| `migration.py` | `atlas/atlas/migration.py` (new) | **The centerpiece.** `reconcile_migrations()` (the scheduler "callback") + `advance_migration()` (the per-phase dispatcher) + the Frappe-side cutover/repoint helpers + lost-Task detection. |
| `migration-export-source.py` | `scripts/` | Source phase: thin-snap both disks + NBD export on localhost. |
| `migration-clone-target.py` | `scripts/` | Target phases: module/image/pool pre-flight, fresh thin LVs, SSH tunnel + nbd client, dm-clone build, and the host-keys-preserved identity re-injection. |
| `migration-poll-hydration.py` | `scripts/` | The Hydrating phase's cheap, re-schedulable probe (`enable_hydration` once + parse `dmsetup status`). |

## The two ideas worth carrying away

**1. Resumability is a row + a scheduler, not a long-held job.** Every phase
records its name as the row's `status`; `reconcile_migrations` re-enters that
phase each tick. A dropped RQ job, a provider rate-limit, an SSH blip, or a
worker crash is survived because the next tick reads the DB and continues — and
because every phase first checks "am I already done?" (its idempotency key)
before acting. The long pole (disk hydration) is a *series of cheap polls*, never
a worker held for minutes. This is the operator's requested "callback so you can
keep going in case of API issues or rate limits", made structural.

**2. Identity is preserved; minimally the address moves.** The UUID and
everything derived from it (MAC/tap/netns/uid/veth) re-materialize identically on
the target. SSH host keys are preserved (`regenerate_host_keys=False`, the
rebuild contract). On the **keep-address** path (Scaleway) only `server` changes
— the `/128` is preserved and routed across hosts (spec §2), so even the
proxy/Subdomain layer needs no re-point. On the **change-address** fallback
(DigitalOcean) `ipv6_address` changes too — through the one sanctioned
`flags.migrating` gate — and the proxy/Subdomain layer is explicitly re-pointed
to the new `/128`.

## What is NOT in these sample files yet (the spec is ahead of the sample)

The open questions are **resolved** — see [spec/24](../../24-vm-migration.md).
These sample files predate two of those resolutions and show the *older* shape;
build from the spec, not the sample, where they differ:

- **The IPv6 `/128` is now preserved across the move on Scaleway** (spec §2,
  resolving the old Q3): the source host routes the `/128` to the target over an
  SSH-carried TUN tunnel until the whole flexible `/64` drains, then the `/64`
  moves with one Scaleway API pair. The sample shows the older *change-address*
  path (new `/128` + Subdomain re-point), which is now the **DigitalOcean
  fallback** only. The keep-address tunnel scripts and the
  `reconcile_block_fip_moves` reconciler are specified in §2 but not yet sampled.
- **The Reserved IP is now preserved across the move** (spec §6, resolving Q1):
  `Reserved IP.server` immutability was relaxed and a `reassign()` method added,
  so a customer's inbound v4 follows the VM. The sample shows the older
  detach-and-drop. `release_reserved_ip=True` is now an explicit *drop* override,
  not the default.

Still as the sample shows (resolutions that matched the sample's defaults):

- Cross-provider migration is out of scope (pre-flight refuses). Region is
  same-by-construction (one region per Atlas instance — the old Q6).
- The data-disk hydration is a second dm-clone, symmetric with root (Q5).
- Boot the target at any hydration %, hold the source NBD export until 100% (Q2).
- One opaque **Migrate** button + the scheduler; per-phase buttons are debug-only (Q4).
