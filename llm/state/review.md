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

## Open (pre-READY)

- Live e2e green (the above). Until then this is implement→review, not READY.
- After green: drop this file + plan.md down to a short note; mark READY in
  active.md.
