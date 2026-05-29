# Plan — ipv4-egress

NAT44 outbound IPv4 for the IPv6-only guest, masqueraded out the host's public
IPv4. Egress-only. Mirrors the existing per-VM IPv6 model one-for-one. No new
DocType, no new DB field, no Python state machine — server-side logic stays in
the shell scripts (Taste 11-13), the only Python is one pure derivation helper.

Intake decisions (from `scratch/active.md`): mechanism = **NAT44** (not NAT64);
all four reject-on-sight bars apply. This plan resolves every open design
question so there are none left to decide during implementation.

---

## The shape, end to end

Today a guest has eth0 with **only** an IPv6 /128, default route `via fe80::1`,
DNS via Cloudflare v6. We add a **private IPv4** to that same eth0 and a v4
default route, then masquerade on the host. Nothing about v6 changes.

```
GUEST eth0:
  ::<vm>/128                 (unchanged)            default via fe80::1   (v6, unchanged)
  100.64.<k>.2/30  (NEW)                            default via 100.64.<k>.1 (v4, NEW)
        |
   tap atlas-<...>           host side: fe80::1/64 (unchanged) + 100.64.<k>.1/30 (NEW)
        |
HOST:  ip_forward=1 (NEW sysctl)
       nft  inet atlas  postrouting chain (NEW):  ip saddr 100.64.0.0/16 oifname <uplink> masquerade
        |  src -> host public IPv4
     INTERNET (any IPv4 destination)
```

### Addressing — derived, stateless, no new field

The guest's private v4 is **never seen on the wire** (it is masqueraded), so it
only needs to be unique *per host*. We derive it from the VM's existing
`ipv6_address` — exactly the spirit of `derive_mac` / `derive_tap` in
`networking.py` — so there is **no new allocator and no new DocType field**.

- Fixed per-host super-net: **`100.64.0.0/16`** (RFC 6598 CGNAT space; chosen
  over RFC1918 `10/8`/`192.168/16` so it will not collide with a Self-Managed
  host's own LAN or with DO's internal addressing).
- `index` = the integer value of the v6 host part = the last hextet of
  `ipv6_address` interpreted as an int (e.g. `…::2` → 2, `…::f` → 15). This is
  the same index the v6 allocator hands out, so the v4 and v6 of one VM share an
  index — easy to correlate in `ip addr`/journal.
- Each VM gets a point-to-point **`/30`** at offset `index * 4` inside the /16:
  - base = `100.64.0.0` + `index * 4`
  - host side = base + 1  (e.g. index 2 → `100.64.0.9`… see worked example)
  - guest side = base + 2
- `/16` holds 16384 `/30`s — far beyond any realistic per-host VM count,
  including a Self-Managed `/64` v6 range. (A `/124` caps at 15.) If `index * 4`
  ever exceeded the /16, the helper raises — fail loud, not silent wrap.

Worked example (index 2): base `= 100.64.0.0 + 8 = 100.64.0.8`, host
`100.64.0.9/30`, guest `100.64.0.10/30`. The guest sees default
`via 100.64.0.9`.

Why /30 point-to-point per tap and not one shared bridge subnet: the existing
model has **no bridge** — each VM is a standalone tap with a /128 host route.
A /30 per tap keeps that "each tap is self-contained, host-side address is the
guest's gateway" property identical to how `fe80::1/64` works today. No new
device type, no bridge lifecycle.

---

## What we are NOT doing

- **No NAT64 / DNS64.** No Tayga/Jool/CLAT, no DNS64 resolver on the host. The
  spec frames that as "above Atlas"; it would add a host daemon (violates "few
  deps / no agent"), break DNSSEC, and fail on v4 literals. Rejected at intake.
- **No inbound IPv4.** No DNAT, no port-forward, no per-VM public v4. The guest
  is reachable from outside over **IPv6 only**, exactly as today. The v4 is
  private and egress-only.
- **No new DocType, no new DB field, no new allocator.** The v4 is derived from
  `ipv6_address`. (`Virtual Machine` keeps its current fields.)
- **No change to the v6 path.** v6 stays the primary address. Every existing v6
  line in the scripts and the guest unit is left byte-for-byte intact; v4 lines
  are *added alongside*.
- **No Python orchestration of NAT.** No new `run_task` calls, no chained tasks.
  The masquerade/route/addr live in the existing idempotent shell scripts.
- **No floating/reserved v4, no v4 firewalling beyond masquerade**, no
  per-tenant egress IP. Out of scope; note as roadmap if it comes up.
- **No bridge / no shared L2.** Per-tap /30 point-to-point only.

---

## Phases

Small, independently verifiable. Each phase ends green (static + unit where
possible) before the next. The **single-bench rule** applies: the live e2e
verification (Phase 6) happens only after the operator flips the tree.

### Phase 1 — `networking.py`: the derivation helper (+ unit tests)

Add one pure function, mirroring `derive_mac`/`derive_tap`:

```python
def derive_ipv4_link(ipv6_address: str) -> tuple[str, str]:
    """(host_side, guest_side) /30 CIDRs for a VM's private NAT44 link.

    Derived from the VM's IPv6 host-part index so no separate allocation or
    DB field is needed. Private (RFC 6598) — egress-only, never on the wire.
    """
```

- Parse `index` from the v6 host part, compute base `= IPv4Network("100.64.0.0/16")[index*4]`,
  return `("100.64.x.y/30", "100.64.x.z/30")` (host, guest).
- Raise `frappe.ValidationError("no IPv4 link capacity")` if `index*4` falls
  outside the /16 — fail loud (Taste 17).
- Module-level constants: `IPV4_EGRESS_SUPERNET = "100.64.0.0/16"`.

Tests live next to code: extend the existing pure-helper coverage. The e2e
module already exercises helpers in `_check_networking_helpers()`
([virtual_machine_provisioning.py:212](../../atlas/tests/e2e/use_cases/virtual_machine_provisioning.py#L212));
add `derive_ipv4_link` assertions there (index 2 → `100.64.0.9/30` host,
`100.64.0.10/30` guest; round-trips for a few indices; out-of-range raises).
**No unit-test DB needed** — pure function, so it also belongs in a fast
`UnitTestCase` if one exists for `networking`; check and prefer that.

Verify: import + call by hand under `bench execute` is *not* needed (pure
Python); a local `python -c` exercising the helper is enough at this phase.

### Phase 2 — host one-time setup: `bootstrap-server.sh`

Add v4 forwarding + the single masquerade rule, alongside the existing v6
scaffold (`bootstrap-server.sh:54-65`):

- Step 4 (sysctl `/etc/sysctl.d/60-atlas.conf`): add
  `net.ipv4.ip_forward = 1`. Keep the existing v6 lines.
- Step 5 (nftables): the existing table is `inet atlas` with a `forward` chain
  (filter hook). Add a **`postrouting`** chain in the same `inet atlas` table
  with `type nat hook postrouting priority srcnat;` and one rule:
  `ip saddr 100.64.0.0/16 oifname <uplink> masquerade`.
  - `<uplink>` = `ip -j route show default | jq -r '.[0].dev'` (the **v4**
    default route dev — note today's scripts query `-6`; the v4 query is the
    correct one for the masquerade rule).
  - Guard idempotently exactly like the forward chain:
    `nft list chain inet atlas postrouting >/dev/null 2>&1 || nft add chain …`,
    then add the rule only if absent (match on a stable substring).

Why `inet atlas` (not a separate `ip atlas` table): `inet` family handles both
v4 and v6; `masquerade` in an `inet` `nat` chain applies to v4 traffic. One
table keeps teardown/inspection in one place (`nft list table inet atlas`).

Verify (static): `bash -n bootstrap-server.sh`; eyeball the nft syntax. Live
re-bootstrap is part of Phase 6.

### Phase 3 — per-VM host networking: `vm-network-up.sh` / `vm-network-down.sh`

`vm-network-up.sh` (after the existing v6 block, `vm-network-up.sh:36-50`):

- Re-assert the postrouting masquerade scaffold idempotently (same defensive
  recreate as the forward chain at `:28-30`, so the first VM unit after a host
  reboot rebuilds it — matches the v6 self-sufficiency contract in
  `spec/06-networking.md:128-134`).
- Re-apply `net.ipv4.ip_forward=1` defensively (next to the v6 sysctl at `:34`).
- Compute the host-side /30 from `VIRTUAL_MACHINE_IPV6` (call out to a tiny
  inline derivation, OR — preferred — read `IPV4_HOST_CIDR` from `network.env`,
  written by Phase 4; decide in favor of `network.env` so the shell does no IP
  math and Python stays the single source of the derivation).
- `ip -4 addr add <IPV4_HOST_CIDR> dev "$TAP_DEVICE"` (host side of the /30).
- No host **route** to the guest /30 is needed beyond the connected route the
  address add creates (point-to-point /30, guest is directly on-link via the
  tap) — confirm in Phase 6; if the guest's specific /32 needs an explicit
  route, add `ip -4 route replace <guest>/32 dev "$TAP_DEVICE"` (kept symmetric
  in down).

`vm-network-down.sh` (symmetric, best-effort, `vm-network-down.sh:22-25`):

- `ip -4 addr del <IPV4_HOST_CIDR> dev "$TAP_DEVICE" 2>/dev/null || true`
  (the tap is deleted right after anyway, so this is belt-and-suspenders).
- The masquerade rule is **host-wide, not per-VM** → it is NOT removed on VM
  teardown (it stays for the next VM, exactly like the v6 forward chain/table
  scaffold is never torn down). Only per-VM forward rules are removed today;
  v4 adds none (the forward chain policy is `accept`), so down stays tiny.

Decision locked: the masquerade rule matches the whole `/16` source, so it is
**one rule for the whole host** — no per-VM nft churn, nothing to delete per VM.

Verify (static): `bash -n` both. Live in Phase 6.

### Phase 4 — pass the v4 to the guest: `provision-vm.sh` + guest config

- `provision-vm.sh` step 4 writes `network.env` (`provision-vm.sh:144-148`).
  Add the derived v4 link values so the shell never computes them:
  `IPV4_HOST_CIDR=…`, `IPV4_GUEST_CIDR=…`, `IPV4_GATEWAY=…` (host side, no
  mask). These come from the controller (Phase 5) via env vars.
- `provision-vm.sh` step 2 writes the guest's `/etc/atlas-network.env`
  (`provision-vm.sh:64-66`, currently only `VIRTUAL_MACHINE_IPV6`). Add
  `VIRTUAL_MACHINE_IPV4=<guest /30 cidr>` and `VIRTUAL_MACHINE_IPV4_GATEWAY=<host side, no mask>`.
- Guest unit `scripts/guest/atlas-network.service` (`atlas-network.service:10-13`):
  add two `ExecStart=` lines **after** the v6 ones, leaving v6 intact:
  - `/usr/sbin/ip addr add ${VIRTUAL_MACHINE_IPV4} dev eth0`
  - `/usr/sbin/ip route add default via ${VIRTUAL_MACHINE_IPV4_GATEWAY} dev eth0`
  - DNS already resolves over v6 (`2606:4700:4700::1111`), so **no resolv.conf
    change** — this idea is about v4 *destinations*, not v4 DNS. (Note in spec.)
- This unit is baked into the rootfs at **image sync time**
  (`sync-image.sh:66`), not provision time. So shipping the new unit requires a
  **re-sync of the image** to each server (operator clicks Sync to Server, or
  the e2e harness's `ensure_image_on_server` rebuilds). Call this out in the
  verify steps — a stale image on the server will silently lack the v4 lines.

### Phase 5 — controller: feed the derived v4 into provisioning

`virtual_machine.py::_provision_variables()` (`virtual_machine.py` ~line 200):
add the three v4 env vars by calling `derive_ipv4_link(self.ipv6_address)`:

```python
host_cidr, guest_cidr = derive_ipv4_link(self.ipv6_address)
... "IPV4_HOST_CIDR": host_cidr,
    "IPV4_GUEST_CIDR": guest_cidr,
    "IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
    "VIRTUAL_MACHINE_IPV4": guest_cidr,
    "VIRTUAL_MACHINE_IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
```

(The two pairs overlap; collapse to the minimal set the two heredocs in
`provision-vm.sh` actually consume — keep names matching the script.) **No new
field, no derivation stored on the row** — computed at provision time from the
already-allocated `ipv6_address`. Purely additive to the variables dict; no
state-machine change, no immutability change.

Verify: the existing `_check_networking_helpers` + a controller-level check
that `_provision_variables()` now contains the v4 keys with sane values (add to
`virtual_machine_provisioning.py`, pure Python, no droplet).

### Phase 6 — e2e proof (the reject-on-sight #3 bar) — REQUIRES BENCH FLIP

The success bar: a **real booted guest reaches a v4-only destination**. The
cleanest home is a new probe script driven from the existing
`virtual_machine_provisioning` happy path (it already SSHes into the guest via
`phase5-guest-identity.sh`):

- New probe `atlas/tests/e2e/scripts/phase5-ipv4-egress.sh`, same idiom as
  `phase5-guest-identity.sh` (SSH in with the ephemeral key, `set +x` to keep
  the key out of stderr). Inside the guest, assert:
  1. eth0 has the expected private v4 (`ip -4 addr show eth0` contains
     `100.64.`).
  2. a v4 **default route** exists (`ip -4 route show default`).
  3. **reach a v4-only destination**: `curl -4 --max-time 10 -sS
     https://1.1.1.1/` (an IPv4 *literal* — forces the v4 path, no DNS, proves
     masquerade end-to-end). Optionally also `ping -4 -c1 1.1.1.1`. Exit
     non-zero with a clear FAIL message otherwise.
- Wire it into `_check_provision_happy_path`
  ([virtual_machine_provisioning.py:126-146](../../atlas/tests/e2e/use_cases/virtual_machine_provisioning.py#L126))
  right after the identity probe, passing `VIRTUAL_MACHINE_IPV6` (for the SSH
  hop) + `SSH_PRIVATE_KEY`.
- **FIX the existing assertion that now contradicts us:**
  `phase5-guest-identity.sh:108-110` asserts *"eth0 has no global IPv4"* — that
  was guarding against the fcnet leftover (`91.83.x.x/30`). With NAT44 the guest
  now legitimately has a `100.64.x.x/30`. Tighten the assertion: fail only on a
  **non-`100.64.` global v4** (i.e. the fcnet leftover), allow the Atlas
  egress v4. Comment why.
- `desk_buttons` coverage: provisioning is exercised via the auto-provision
  path; no new *button* is added (egress is automatic), so per the spec
  "Bias toward adding a check to an existing use case … add a new use-case
  module only when the operator gets a new button" — **no new desk_buttons
  entry**; the v4 check rides the existing Provision flow. (State this in the
  spec update so the absence is deliberate, not an omission.)

Stop after writing all of Phase 6's tests and say: *"ready to verify —
`atlas-tree ipv4-egress` when free."* Operator flips, e2e runs against a real
droplet (must re-sync the image so the new guest unit lands — Phase 4 note).

### Phase 7 — spec update (reject-on-sight #2) — at READY/merge

Rewrite to describe NAT44 egress as built (spec is source of truth):

- `spec/06-networking.md`: the doc is currently "IPv6 only, sidestep v4."
  - Title/intro (`:1-12`): keep IPv6 as the public/inbound story; add an
    **"IPv4 egress (NAT44)"** section describing the derived /30, the
    `100.64.0.0/16` supernet, the host masquerade rule, and the guest config.
  - "Host-side configuration" (`:104-134`): add `net.ipv4.ip_forward=1` and the
    `postrouting` masquerade chain to the documented host state.
  - "Per-VM, on the host" (`:136-160`): add the v4 addr-on-tap step.
  - "Inside the guest" (`:162-179`): add the v4 addr + default route; note DNS
    stays v6.
  - "Verifying connectivity" table (`:182-194`): add a v4-egress row
    (symptom: v6 works, v4 curl fails → check masquerade rule / ip_forward).
  - "What we do not do" (`:221-228`): **rewrite** "No IPv4 in the guest.
    Reaching v4-only services… is a future problem" → "Inbound is IPv6-only; v4
    is egress-only via host NAT44. No inbound v4, no per-VM public v4."
- `spec/README.md` non-goal (`:27`): "No private networking, no overlay, no
  IPv4 to the guest." → narrow to "no inbound IPv4 to the guest; outbound IPv4
  is via host NAT44" (or move to Goals: "Give each VM outbound IPv4 via NAT").
  Also add a Goals bullet near `:21`.
- `spec/03-bootstrapping.md` (`:36`, step 4/5 summary): mention ip_forward +
  masquerade chain.
- `spec/07-filesystem-layout.md`: `network.env` now also carries the v4 link —
  update the inline comment (`:17`).
- `spec/09-roadmap.md`: if anything v4-related was deferred there, move it to
  "done"; otherwise add nothing. (Per-tenant egress IP / inbound v4 / v4
  firewalling are legitimately still future — add a one-liner only if not
  already implied.)

---

## Files touched (the whole blast radius)

| File | Change | Phase |
| --- | --- | --- |
| `atlas/atlas/networking.py` | + `derive_ipv4_link()` + `IPV4_EGRESS_SUPERNET` | 1 |
| `scripts/bootstrap-server.sh` | + `ip_forward` sysctl, + postrouting masquerade chain | 2 |
| `scripts/vm-network-up.sh` | + v4 sysctl re-apply, + masquerade re-assert, + tap v4 addr | 3 |
| `scripts/vm-network-down.sh` | + tap v4 addr del (best-effort) | 3 |
| `scripts/provision-vm.sh` | + v4 vars into `network.env` and guest `/etc/atlas-network.env` | 4 |
| `scripts/guest/atlas-network.service` | + 2 ExecStart lines (v4 addr + default route) | 4 |
| `atlas/atlas/doctype/virtual_machine/virtual_machine.py` | + v4 vars in `_provision_variables()` | 5 |
| `atlas/tests/e2e/scripts/phase5-ipv4-egress.sh` | NEW probe | 6 |
| `atlas/tests/e2e/scripts/phase5-guest-identity.sh` | relax "no v4 on eth0" → "no non-100.64 v4" | 6 |
| `atlas/tests/e2e/use_cases/virtual_machine_provisioning.py` | wire probe + helper assertions | 1,5,6 |
| `spec/06,03,07,09,README` | document NAT44 as built | 7 |

No DocType JSON changes. No new files except the one probe script and (if not
already present) nothing else. ~zero new Python beyond one helper + a few dict
keys — the bulk is shell, per Taste 11-13.

---

## Risks / things to confirm at verify time (Phase 6, live)

1. **Connected-route sufficiency.** A /30 host addr on the tap should create a
   connected route covering the guest; if not, add an explicit `/32` route
   (planned fallback, kept symmetric). Confirm with `ip -4 route` on the host.
2. **Uplink detection for v4.** `ip -j route show default | jq -r '.[0].dev'`
   must return the public-v4 NIC. On DO that's `eth0`; on a multi-homed
   Self-Managed host confirm `.[0]` is the internet-egress route.
3. **Image re-sync required.** The new guest unit is baked at sync time; a
   server with a stale image won't have the v4 lines. Re-sync before the e2e
   asserts egress (the harness `ensure_image_on_server` should rebuild — verify
   it actually re-runs sync when the unit file changed; if it short-circuits on
   "rootfs already built" at `sync-image.sh:47-50`, the e2e will test a stale
   image — may need to bump image name or force re-sync in the test).
4. **MTU.** virtio-net + masquerade should be fine at 1500; if v4 egress to some
   hosts hangs on large packets, suspect PMTU — note only, don't pre-engineer.
5. **Self-Managed host with NAT'd/elastic public v4.** Masquerade out the
   default-route uplink works regardless of whether the host's own v4 is public
   or itself NAT'd upstream — no special-casing.

## Done bar (gate to READY)

- [ ] `derive_ipv4_link` + unit assertions green (Phase 1).
- [ ] All scripts `bash -n` clean; guest unit valid (Phase 2-4).
- [ ] Controller passes v4 vars; helper-level e2e checks green (Phase 5).
- [ ] **Live e2e**: a booted guest curls a v4 literal successfully; v6 still
      reachable; `phase5-guest-identity` passes with the relaxed assertion
      (Phase 6, after bench flip).
- [ ] Spec rewritten; no doc still says "no IPv4 to the guest" unqualified
      (Phase 7).
- [ ] `llm/state/` near-empty (only this plan + a short review note).
