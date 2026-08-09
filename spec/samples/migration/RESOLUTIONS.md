# VM migration — resolved questions

The design pass left six genuinely-open questions for the operator. **All six
are now resolved in the spec** ([spec/24](../../24-vm-migration.md)); this file
records each one, the decision, and where the spec makes it authoritative. It
supersedes the earlier `OPEN-QUESTIONS.md` (nothing here is still open).

Two resolutions **reversed** the sample's original defaults — the sample files in
this directory still show the *older* shape and are behind the spec on those two
points (called out in the [README](./README.md)). Build from the spec where they
differ.

---

### Q1 — Reserved IP across hosts → **RESOLVED: preserve it (reassign at the vendor).** *(reverses the sample)*

The customer's inbound v4 now **survives the move**. `Reserved IP.server`
immutability was **relaxed** — the IP is bound to its address + vendor handle for
life, but which Server it points at is a mutable pointer (and it may rest with no
Server at all). A new `reassign(target_server)` method moves the IP at the vendor
(`assign_reserved_ip(handle, target_droplet)`) and repoints the row. The
`Repointing` phase does `detach()` on source → `reassign(target)` → `attach(vm)`
on target, so the IP and any DNS A record are unchanged.

`release_reserved_ip=True` is now an explicit **drop-it** override (free the
address instead of moving it); the default is **preserve**, not drop.

→ [spec/24 §6](../../24-vm-migration.md#6-reserved-ip-public-ipv4), [02 § Reserved IP](../../02-doctypes.md#reserved-ip)
*(Sample still shows detach-and-drop — the pre-relaxation shape.)*

---

### Q2 — Hydration threshold + NBD lifetime → **RESOLVED: boot at any %, hold source until 100%.** *(matches the sample)*

The target **boots at any hydration %** (reads-through to the source over NBD),
but the source NBD export + snapshot are **held alive until hydration hits
100%**, and `Cleanup` runs only after each dm-clone is collapsed. This gives fast
availability *and* a clean rollback window — the source VM stays intact and
re-startable through `CutoverStarting`.

→ [spec/24 §5 — Hydration / Collapse+cutover / Cleanup](../../24-vm-migration.md#5-storage-nbd-export--dm-clone-hydration)

---

### Q3 — Is the IPv6 prefix portable? → **RESOLVED: yes — the `/128` is preserved across the move.** *(reverses the sample; the biggest change)*

The earlier design let the `/128` always change and re-pointed the proxy. The
spec now **keeps the `/128`**, by two provider-specific mechanisms:

- **Scaleway (range moves).** The VM range is a portable routed flexible `/64`.
  The source host routes the VM's `/128` to the target over a host-to-host TUN
  tunnel for the transition window; once every VM on that `/64` has migrated or
  terminated, the whole `/64` moves to the target with one provider API pair.
- **DigitalOcean (permanent per-VM forward).** The carved `/124` is **not**
  portable, so the source host keeps answering proxy-NDP for the address and
  tunnels it to the target **permanently** (one `tun` device per migrated VM).
- **Self-Managed** falls through to the change-address path unless the operator
  wires BGP re-announce.

Because the `/128` is preserved, `derive_ipv4_link` is unchanged (no
network-env re-injection), and the **proxy/Subdomain re-point + `reconcile_region`
are eliminated** on the keep-address paths — they survive only on the
change-address fallback.

→ [spec/24 §2](../../24-vm-migration.md#2-the-ipv6-128-cross-host-routing-scaleway-keep-address-path) (Scaleway), [§2.9](../../24-vm-migration.md#29-permanent-per-vm-forwarding-digitalocean-keep-address-path) (DigitalOcean), [§2.8](../../24-vm-migration.md#28-self-managed-fallback--portability-detection) (Self-Managed / detection)
*(Sample shows the change-address path, now the DO/Self-Managed fallback only.)*

---

### Q4 — Migrate UX → **RESOLVED: one button + the scheduler.** *(matches the sample)*

One **Migrate** button on the VM form creates the `Virtual Machine Migration` row
(target picker + optional `release_reserved_ip` ack); the scheduler drives it.
The Migration form shows the phase pill, hydration %, `tunnel_status`, and a
**Retry** on `Failed`. Per-phase manual buttons are debug-only; the lifecycle
guard blocks concurrent lifecycle actions mid-migration.

→ [spec/24 §8](../../24-vm-migration.md#8-operator-ux-resolved-one-button--scheduler)

---

### Q5 — Data disk copy strategy → **RESOLVED: a second parallel dm-clone.** *(matches the sample)*

The data disk migrates as a **second dm-clone over a second NBD export** (root =
`nbd_port`, data = `nbd_port+1`), symmetric with the root disk — same idempotency
+ hydration-poll machinery, and the data disk is available immediately too. The
blocking-`dd` alternative was rejected.

→ [spec/24 §5 — Data disk](../../24-vm-migration.md#5-storage-nbd-export--dm-clone-hydration)

---

### Q6 — Server → region mapping → **RESOLVED: not needed — one region per Atlas instance.** *(matches the sample)*

Each Atlas instance operates in **one region**, so a migration's source and
target are always same-region by construction. There is no cross-region case to
guard; `Subdomain.region` (immutable) and the Reserved-IP region binding are
satisfied trivially; the VM's `region` field is copied from the source verbatim.
No `Server.region` field or mapping is required. (The region fields are slated
for removal once the single-region invariant is made structural.)

→ [spec/24 §1 — Region note](../../24-vm-migration.md#why-this-shape)

---

## The non-question flag → **RESOLVED: folded into bootstrap.**

The design's two new host dependencies (`qemu-nbd`/`nbd-client` userspace +
`nbd`/`dm_clone` kernel modules) are now **installed by `bootstrap-server.py`**,
so every Active host can be a migration source or target with no re-bootstrap:
`qemu-utils` + `nbd-client` join the apt set, and a dedicated step installs
`linux-modules-extra-$(uname -r)` (pinned to the running kernel) and loads +
persists the modules via `/etc/modules-load.d/60-atlas-migration.conf`. The
target clone-script still re-asserts them defensively in its pre-flight.

→ [spec/24 § New dependencies](../../24-vm-migration.md#new-dependencies), [README § principle 5](../../README.md), [03-bootstrapping.md](../../03-bootstrapping.md)
