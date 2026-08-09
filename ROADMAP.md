# Atlas roadmap

This maps every open issue in `frappe/atlas` to a completion status, grouped by
theme, with one line of context against what Atlas ships **today**.

- **Scope:** the 89 open issues as of 2026-07-14, assessed on branch
  `feat/vm-service-seam`.
- **Source of truth for status:** the [spec](./spec/README.md) (what the system
  *is*) cross-checked against the code. Status is a judgement call, not a label
  on the issue — issues that ask for *more* on top of a shipped base are marked
  In progress / Planned, not Shipped.
- **Not the same file as** [`spec/09-roadmap.md`](./spec/09-roadmap.md), which is
  the curated *deferred-decisions* narrative (punts + near-term hedges). This
  file is the issue-by-issue status board.

## Legend

| Badge | Meaning |
|-------|---------|
| ✅ Shipped | Implemented; the spec describes it as current behavior |
| 🟡 In progress / partial | A base ships; the issue's ask is not fully done, or it's being built now |
| ⚪ Planned | Clear intent, no implementation yet |
| 🔵 Deferred / evaluating | Explicitly punted, optional, or an open design question |
| 🐛 Bug | A concrete defect, not roadmap work |
| ♻️ Duplicate / stale | Redundant with another issue, or overtaken by a decision |

## Status rollup

| Status | Count | Issues |
|--------|------:|--------|
| ✅ Shipped | 1 | #18 |
| 🟡 In progress / partial | 26 | #16 #35 #41 #46 #47 #48 #49 #50 #57 #61 #65 #67 #68 #73 #74 #77 #81 #82 #84 #87 #88 #104 #106 #108 #112 #113 |
| ⚪ Planned | 47 | #17 #31 #33 #34 #37 #38 #39 #40 #42 #43 #51 #54 #55 #56 #58 #59 #60 #62 #66 #69 #70 #71 #72 #76 #78 #79 #83 #85 #86 #90 #92 #93 #94 #95 #96 #97 #99 #100 #101 #102 #103 #105 #107 #109 #110 #111 #114 |
| 🔵 Deferred / evaluating | 12 | #32 #36 #44 #45 #53 #63 #64 #75 #80 #89 #91 #98 |
| 🐛 Bug | 1 | #28 |
| ♻️ Duplicate / stale | 2 | #15 #52 |

The shape of the tracker: ~1 shipped, a cluster of active decoupling/quality
work, and a long tail of planned infrastructure primitives. The center of
gravity right now is the **core ↔ service boundary** ([spec ch.28](./spec/28-core-service-boundary.md)).

---

## A. Decentralization — controller → host

Today's model: controller is smart, hosts are dumb, and **the Frappe DB is the
source of truth; the host is a rebuildable cache** ([ch.01](./spec/01-architecture.md)).
This cluster inverts that and is gated on one unresolved decision (see
Decisions §1).

| # | Issue | Status | Context vs today |
|---|-------|--------|------------------|
| 32 | Decentralize Atlas | 🔵 Deferred | Controller as dumb relay, hosts derive everything (UUIDs vs series). Architectural fork. |
| 34 | Tasks on hosts | ⚪ Planned | Fire-and-forget + status endpoint; today the controller holds a live SSH connection per Task. |
| 36 | Track state on hosts | 🔵 Deferred | Hosts authoritative (sqlite/systemd). **Contradicts DB-as-source-of-truth.** |
| 92 | Host co-ordination | ⚪ Planned | Hosts negotiate migration ports themselves; today the controller brokers. |
| 95 | Private-net rewiring w/o controller | ⚪ Planned | On-host AllowedIPs updates; [ch.25](./spec/25-private-networking.md) has the controller drive this in phase 1. |
| 96 | Atlas bootstrap | ⚪ Planned | Hosts self-bootstrap; today a controller-driven SSH script. |
| 97 | Controller bootstrap | ⚪ Planned | Host spawns the controller VM; today controller is a hand-set special case. |

## B. Core ↔ service boundary — "Break X" ([ch.28](./spec/28-core-service-boundary.md))

The active workstream. `vm_services.py` (the VMService registry/seam) is wired
on this branch; the fan-out still lives in the generic VM controller.

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 35 | Remove Central privileged auth | 🟡 In progress | Central-managed tunnel + scoped service user ship ([ch.21](./spec/21-tunnel.md)); signed webhooks replacing creds not done. |
| 49 | Break Pilot | 🟡 In progress | Pilot-agnostic core; being unentangled via the satellite seam. |
| 50 | Break Central | 🟡 In progress | Move Central credential injection out of core. |
| 106 | Break Proxy | 🟡 In progress | Pull proxy/TLS out; core keeps only v4 + privileged-routing primitives. |
| 108 | Reduce Pilot boot comms | 🟡 In progress | Kill cold/warm "boot→wait→SSH→change". |
| 112 | Better Signups (umbrella) | 🟡 In progress | Ties #49/#108/#111 + Pilot issues. |
| 113 | No Boot+Wait+SSH+Change (umbrella) | 🟡 In progress | Remove every wait-then-SSH-mutate case. |
| 114 | Secret / Config injection | ⚪ Planned | `secrets.py` does storage indirection; pre-boot file/token injection not built. Preferred fix for #113. |

## C. Compute / VM performance

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 18 | Create Plain Ubuntu VM | ✅ Shipped | The core lifecycle capability. **Candidate to close.** |
| 16 | Allocation | 🟡 In progress | `placement.py` + `overprovision_factor` capacity model ship; full per-node accounting overlaps the placement/packing work. |
| 31 | Sleepy VMs | ⚪ Planned | Stop idle VMs, wake on inbound packet. No idle-stop today. |
| 48 | Fast boot | 🟡 In progress | Firecracker boots fast; the <1s/<2s/<5s **perf gates** aren't codified as tests. |
| 85 | Ballooning | ⚪ Planned | No ballooning today. |
| 86 | Burst CPU | ⚪ Planned | cgroup CPU **caps** ship; temporary burst not. |
| 87 | Resource rate limits | 🟡 In progress | cgroup mem/CPU caps ship; io/net/block-device limits not. |
| 88 | Faster migrations | 🟡 In progress | Migration ships (~20s downtime, [ch.24](./spec/24-vm-migration.md)); <5s target not met. |
| 93 | UFFD for faster clones | ⚪ Planned | New capability. |
| 110 | Secure clones | ⚪ Planned | Snapshot uniqueness/security; pairs with #75/#93. |
| 75 | Portable memory snapshots | 🔵 Deferred | **Contradicts** the "memory snapshots never transportable between hosts" non-goal. |

## D. Networking

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 104 | Fallback / secondary proxy | 🟡 In progress | 2–3 proxies/region ship ([ch.12](./spec/12-proxy.md)); graceful rebuild-without-disruption not. |
| 77 | Make hosts tenant-aware | 🟡 In progress | `vpc_guard` BPF enforces the tenant /48 on-host ([ch.25](./spec/25-private-networking.md)); confirm residual tenant-id propagation gap. |
| 38 | Authoritative DNS | ⚪ Planned | Route 53 today (`route53_settings`). |
| 83 | Private DNS | ⚪ Planned | VM-name → private IP; [ch.25](./spec/25-private-networking.md) deferred phase. |
| 90 | Proxy API | ⚪ Planned | HTTP endpoint vs SSH+curl-on-socket today. |
| 94 | IPv6 blocks as Reserved IPs | ⚪ Planned | v6 /124 tracked on `Server` today, not in `Reserved IP`. |
| 99 | Reserve addresses | ⚪ Planned | Reserve v6 for DNS/WG/NAT/controller from the /124. |
| 105 | Operator VMs | ⚪ Planned | Privileged tenants (reach host APIs). #109 depends on this. |
| 109 | Proxy routing on private iface | ⚪ Planned | Proxy dials VMs over the private plane; depends on #105. |
| 111 | Static proxy routing | ⚪ Planned | Static `*-{vm-id}` map so the proxy never reloads (TLS-cert limits block the cleaner form). |
| 56 | Atlas peering | ⚪ Planned | Cross-region host talk (snapshot transfer, replicated backups). |
| 45 | Egress NAT through VM | 🔵 Deferred | Host NAT44 ships ([ch.06](./spec/06-networking.md)); routing via VM is optional ("bad for simplicity"). |
| 89 | Router VM | 🔵 Deferred | Move IP routing off host into a VM. Same tradeoff as #45. |

## E. Storage & data durability

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 41 | Coordinated drain | 🟡 In progress | Migration ships; drain-all-VMs orchestration not. |
| 15 | Object storage (rmehta) | ♻️ Duplicate | Same as #78. |
| 78 | Object storage | ⚪ Planned | No object store today; ties Atlas to a service (tension w/ "lowest layer"). |
| 79 | Image store | ⚪ Planned | Store for images/backups/snapshots. |
| 39 | Replicated disks | ⚪ Planned | Local storage only today; keep replication optional. |
| 40 | Replicated snapshots | ⚪ Planned | Spread snapshot copies to survive host loss. |
| 100 | Offsite backups | ⚪ Planned | Copy snapshots to another region / 3rd-party. |
| 71 | Tests for data retention | ⚪ Planned | Graceful shutdown ships; retention tests not. |

## F. Security & hardening

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 46 | Operator VPN | 🟡 In progress | VPN broker + firewall + tunnel ship ([ch.19](./spec/19-vpn-broker.md)/[20](./spec/20-firewall.md)/[21](./spec/21-tunnel.md)); WG-gated host SSH not. |
| 57 | Encryption everywhere | 🟡 In progress | HTTPS + WireGuard private plane ship; at-rest disk enc not. |
| 73 | CIS benchmark tests | 🟡 In progress | Host gets CIS sysctls at bootstrap ([ch.03](./spec/03-bootstrapping.md)); conformance suite not. |
| 58 | Disk encryption | ⚪ Planned | No LUKS today. |
| 44 | Lock down hosts | 🔵 Deferred | All-connectivity-via-VMs; optional, "bad for simplicity". |

## G. Images & VM metadata

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 74 | Image builder | 🟡 In progress | `image_builder.py`/`image_recipes.py` ship for bench/proxy ([ch.15](./spec/15-image-builder.md)); a simple user-facing builder not. |
| 54 | IMDS | ⚪ Planned | Inject VM info via IMDSv2 vs image-tampering today. |
| 55 | OS images | ⚪ Planned | Ubuntu 24.04 only today. |
| 72 | Refresh Ubuntu images | ⚪ Planned | Hardcoded release today. |
| 76 | Smaller images | ⚪ Planned | Ubuntu Minimal today; try Ubuntu Base / drop ZFS. |
| 53 | cloud-init | 🔵 Evaluating | Author flags "maybe too slow for boot targets" (competes w/ #114). |

## H. Provider support

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 37 | Remove provider dependency | ⚪ Planned | Reduce provider-API calls (reserved-IP relocate). Pairs with #43. |
| 62 | AWS support | ⚪ Planned | DO / Scaleway / Self-Managed / Fake today. |
| 63 | Hetzner support | 🔵 Deferred | Deprioritized (pricing). |
| 64 | ovh support | 🔵 Evaluating | Maybe, if cSpace can't meet demand. |

## I. Observability

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 42 | Metrics | ⚪ Planned | No metrics today (`journalctl` is enough, per non-goals). |
| 101 | SLOs | ⚪ Planned | No SLO tracking today. |
| 102 | Public status page | ⚪ Planned | None today. |
| 107 | Minimal metrics for Ubuntu VMs | ⚪ Planned | Optional collector for plain VMs. |

## J. Interfaces — API / CLI / UI / console

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 47 | Console access | 🟡 In progress | `ssh_console` doctype/module ships; browser terminal not. |
| 82 | API | 🟡 In progress | Whitelisted Central methods + `api/` ship; a frozen public API contract not. |
| 33 | Docker | ⚪ Planned | [ch.27](./spec/27-docker-compat.md) drafted; no implementation. |
| 51 | Atlas ui | ⚪ Planned | Desk is read-mostly; make it a writable single-host admin panel. |
| 69 | Host CLI | ⚪ Planned | Loose scripts today (a `scripts/pyproject.toml` exists); no packaged CLI. |
| 70 | Nice CLI verbs | ⚪ Planned | Depends on #69. |
| 103 | Operator CLI | ⚪ Planned | Remote task exec over the API; depends on #82/#91. |
| 52 | frappe-ui ghost | ♻️ Stale | The frappe-ui `/dashboard` SPA was **retired**; reconcile before acting. |

## K. Code quality, tests, and the one real bug

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 28 | migration `_fail()` TOCTOU crash | 🐛 Bug | `DoesNotExistError` aborts the whole reconcile loop; twin at `export.py:475`. Concrete, diff-sized fix. |
| 65 | Module breakdown | 🟡 In progress | Ongoing; ch.28 is one instance. |
| 67 | Integration tests | 🟡 In progress | Suite exists; dataclass/interface work remains. |
| 68 | Improve fake implementation | 🟡 In progress | Fake provider ships but drifted; use as integration reference. |
| 81 | Refactor | 🟡 In progress | Ongoing cruft reduction. |
| 84 | Tests | 🟡 In progress | Decent suite; perf/security expansion planned. |
| 43 | Primitive breakdown | ⚪ Planned | Provider ABC exists; compute/storage/net interfaces w/ mocks not. |
| 59 | Consistent naming | ⚪ Planned | Remove forgotten/unnecessary fields. |
| 60 | Reduce verbosity | ⚪ Planned | Terseness pass. |
| 66 | Separate logic from side-effects | ⚪ Planned | Helper layer for `frappe.get_all` etc. |

## L. Docs & product

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 61 | Documentation | 🟡 In progress | Spec is extensive; user-facing docs not. |
| 17 | PDF generation service (chromium) | ⚪ Planned | A *workload*; tension w/ "lowest layer". |

## M. Cross-cutting decisions (see below)

| # | Issue | Status | Context |
|---|-------|--------|---------|
| 80 | Evaluate swap instead of sleepy VMs | 🔵 Evaluating | Explicit alternative to #31. |
| 91 | Evaluate SSH vs HTTP | 🔵 Evaluating | Undecided transport under #34/#90/#103. |
| 98 | Workflow for long-running tasks | 🔵 Deferred | The "one Task = one shell script" punt ([09-roadmap](./spec/09-roadmap.md)) defers exactly this. |

---

## Decisions to resolve (these gate other issues)

1. **Where does state live? (#32/#36 vs the core principle.)** "Track state on
   hosts" negates "the Frappe DB is the source of truth; the host is a cache."
   This gates #34/#92/#95/#96/#97. Resolve in the spec before building any of them.
2. **Sleepy VMs vs swap (#31 vs #80).** Same problem, opposite mechanisms —
   pick one.
3. **Transport: SSH vs HTTP (#91) underneath #34/#90/#103.** The API-first
   issues assume a control surface #91 hasn't chosen.
4. **Break services *out* (#49/#50/#106) vs add services *in* (#17/#78/#15/#16).**
   The rmehta feature issues are the older "services" framing that ch.28 moves
   away from. Decide per issue whether it belongs in `satellite`, not core.
5. **Portable memory snapshots (#75)** contradicts a documented non-goal; #56
   and #100 lean on the same reversal. Changing it is a deliberate spec edit.
6. **Freeze the API (#82) vs rename things (#59/#70).** Clean up names before
   freezing, or #82 blocks the cleanup.
7. **Config injection has three overlapping designs: cloud-init (#53) /
   IMDS (#54) / pre-boot secret injection (#114).** Reconcile into one story;
   #53 is self-flagged as possibly too slow for the #48 boot targets.

## Housekeeping

- **Close as done:** #18 (core capability shipped).
- **Close as duplicate:** #15 → #78.
- **Reconcile / likely close:** #52 (SPA retired); re-scope #77 and #46 against
  what already ships.
- **Fast follow (bug):** #28 — and file its twin at `export.py:475`.
