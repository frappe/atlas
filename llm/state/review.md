# Review notes — ipv4-egress

Status: all 7 phases coded + spec rewritten early (operator asked for spec
before tests). Static checks green (`py_compile`, `bash -n` on every touched
script). Tests written, **not run** — awaiting bench flip to verify.

## What was built (blast radius)

| File | Change |
| --- | --- |
| `atlas/atlas/networking.py` | `derive_ipv4_link()` + `IPV4_EGRESS_SUPERNET = 100.64.0.0/16` |
| `scripts/bootstrap-server.sh` | `net.ipv4.ip_forward=1`; `inet atlas` postrouting nat chain + one host-wide masquerade rule |
| `scripts/vm-network-up.sh` | re-assert masquerade scaffold + ip_forward; host /30 addr on tap; v4 uplink via `ip -j route show default` |
| `scripts/vm-network-down.sh` | comment: tap deletion drops the v4 addr; masquerade is host-wide, never per-VM teardown |
| `scripts/provision-vm.sh` | v4 vars into guest `/etc/atlas-network.env` + host `network.env` |
| `scripts/guest/atlas-network.service` | +2 ExecStart: v4 addr + v4 default route (after the v6 lines) |
| `atlas/atlas/doctype/virtual_machine/virtual_machine.py` | `_provision_variables()` derives + passes v4 link vars |
| `atlas/tests/e2e/scripts/phase5-ipv4-egress.sh` | NEW probe: guest has 100.64 v4 + v4 default route + curls a v4 literal |
| `atlas/tests/e2e/scripts/phase5-guest-identity.sh` | relaxed step 7: allow 100.64 v4, still fail on fcnet leftover |
| `atlas/tests/e2e/use_cases/virtual_machine_provisioning.py` | wire egress probe; helper assertions; `_provision_variables` v4 keys; exhaust-row image from DEFAULT_IMAGE |
| `atlas/tests/e2e/use_cases/image_sync.py` | image looked up via `DEFAULT_IMAGE["image_name"]` not literal |
| `atlas/tests/e2e/_config.py` | `DEFAULT_IMAGE["image_name"]` → `ubuntu-24.04-v2` (forces rootfs rebuild) |
| `spec/06,03,07,README` | NAT44 egress documented as built; no unqualified "no IPv4 to the guest" remains |

## Decisions made during implement (not in original plan)

1. **v4 derivation = low 14 bits of the v6 address** → /30 at offset `index*4`
   in `100.64.0.0/16`. ::2 → host `100.64.0.9/30`, guest `100.64.0.10/30`.
   16384 links; provably can't overflow the /16 with the mask (the `raise` is
   defensive only). No new DB field, no allocator — pure `derive_ipv4_link`.
2. **Image name bump `ubuntu-24.04` → `ubuntu-24.04-v2`** in the e2e
   `DEFAULT_IMAGE`. Necessary because the guest unit is baked into the rootfs
   and `sync-image.sh` short-circuits on an existing ext4 (`:46-50`) — a reused
   e2e droplet would otherwise test a STALE guest with no v4 lines. Images are
   immutable by contract (changed rootfs = new name), so this is model-correct,
   not a hack. Fixed the two other e2e literals to source the name from
   `DEFAULT_IMAGE` (`image_sync.py:160`, exhaust row) so the bump is contained.
3. **`atlas/bootstrap.py` left at `ubuntu-24.04`** (operator decision). It
   targets fresh servers with no prior rootfs, so sync rebuilds regardless and
   the new guest unit lands; the stale-rootfs hazard doesn't apply to the
   one-shot. Trade-off: an operator who previously ran the OLD bootstrap keeps
   the stale guest unit on that server until they sync a new image name.

## CTO lens — upsides / downsides

- **Upside:** mirrors the v6 model exactly (per-tap point-to-point, derived not
  allocated, host-wide nft scaffold re-asserted by the first VM unit). Tiny
  Python surface (one pure helper + a few dict keys); all the work is shell, per
  Taste 11-13. No new DocType/field/state. v6 path untouched.
- **Downside / pre-existing fragility surfaced:** `sync-image.sh`'s "rootfs
  already built → exit 0" makes ANY guest-unit change invisible to an
  already-synced server. We work around it via the immutable-image
  name-bump contract, but the broader smell is that the guest unit's version
  isn't tied to anything the short-circuit checks. Roadmap candidate: key the
  short-circuit on a content digest, or stamp a guest-unit version into the
  image row. NOT doing it now (out of scope; the name-bump contract is the
  documented escape hatch).
- **Self-Managed:** masquerade out the v4 default-route uplink works whether the
  host's own v4 is public or itself upstream-NAT'd. No special-casing.

## Verify checklist (after `atlas-tree ipv4-egress`)

Single-bench rule: operator flips the tree, then runs the e2e. The image bump
means the FIRST run rebuilds the rootfs on the shared droplet (~minutes).

1. Re-bootstrap the shared server (picks up ip_forward + masquerade chain), or
   let `ensure_bootstrapped_server` do it. Confirm on host:
   - `sysctl net.ipv4.ip_forward` → 1
   - `nft list chain inet atlas postrouting` shows `ip saddr 100.64.0.0/16 … masquerade`
2. `bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.virtual_machine_provisioning.run`
   (or `run_all`). The happy path now runs `phase5-ipv4-egress.sh`:
   - guest `ip -4 addr show eth0` → a `100.64.x.x/30`
   - guest `ip -4 route show default` → via `100.64.x.x`
   - guest `curl -4 https://1.1.1.1/` succeeds (proves masquerade end-to-end)
   - `phase5-guest-identity.sh` still passes with the relaxed v4 assertion
   - v6 reachability unchanged (identity probe hops in over v6)
3. Watch for the risks from `plan.md`:
   - connected /30 route reaches the guest (if not, add explicit `/32` route in
     vm-network-up.sh — kept symmetric in down)
   - `ip -j route show default` returns the right v4 uplink on the droplet
   - the rootfs actually rebuilt (new image name) — `phase4-probe`/layout shows
     `/var/lib/atlas/images/ubuntu-24.04-v2/`

## Merge with main (vm-features: snapshot/rebuild/resize/pause/clone)

Merged `main` (3 commits, incl. `3ecdf6f` VM lifecycle) into the branch.
Conflicts resolved + v4 re-wired for the new VM-creation paths:

- **`provision-vm.sh` step-2 refactor**: identity injection moved into
  `scripts/lib/prepare-rootfs.sh::atlas_inject_identity`. Took main's structure;
  added two REQUIRED params (`IPV4_GUEST_CIDR`, `IPV4_GATEWAY`) to that function
  and write them into the guest `/etc/atlas-network.env`. So **provision, clone,
  and rebuild all get v4 for free** (they all call atlas_inject_identity).
- **`rebuild-vm.sh`**: now requires + forwards the two v4 vars (it rewrites the
  guest env in place, so it must re-inject v4 or the rebuilt guest loses it).
- **`resize-vm.sh`**: untouched — only edits firecracker.json + grows the
  rootfs, never the network env. No v4 concern.
- **controller**: extracted `_ipv4_link_variables()` (dedup) used by both
  `_provision_variables()` (clone flows through this) and `_rebuild_variables()`.
  `clone_to_new_vm` inserts a normal VM row → auto_provision → _provision_variables,
  so the clone derives its OWN v4 from its OWN fresh ipv6 — distinct /30, no extra
  wiring. Composes cleanly with main's IMMUTABLE/RESIZE_MUTABLE split and the
  new Paused status (v4 is orthogonal to the state machine).
- **e2e**: added the egress probe to the rebuild path in
  `virtual_machine_snapshot.py` (highest-risk: in-place env rewrite). Clone +
  fresh-provision egress covered via the shared atlas_inject_identity code path.
- **spec**: updated `05-virtual-machine-lifecycle.md` fit-and-finish bullet
  ("No global IPv4 on eth0" → "only the 100.64 egress v4"); `07` network.env
  line now carries both the snapshots/ tree (main) and IPV4_HOST_CIDR (mine);
  `03` summary auto-merged with both the apt-lock wait and my v4 steps.

Static checks green post-merge (py_compile all touched Python; bash -n all
touched scripts; zero conflict markers).

## Open (pre-READY)

- Live e2e green (the above). Until then this is implement→review, not READY.
- After green: drop this file + plan.md down to a short note; mark READY in
  active.md.
