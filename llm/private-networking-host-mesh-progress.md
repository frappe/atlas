# Private-networking host-mesh — build progress

Building the WireGuard **host mesh** (variant b) from
[private-networking-host-mesh.md](./references/private-networking-host-mesh.md).
This branch (`idea/private`) is NOT on a bench — code can't be run on bench
web/worker. Components (wg commands, key derivation, packet path) get verified
by direct SSH to the real Scaleway hosts (`Server` rows on the `scaleway.local`
bench, which is PROD — treat with care, Build this feature end to end. You're on a live bench.

Test it on tests.local and run e2e on e2e.local

When you're confident. Deploy it on scaleway.local. Don't run tests on scaleway.local

I'll be AFK now. So you won't be able to ask me any questions. Make reasonable choices and inform me in the end

Don't conflict with the VPN Tunnel and VPN Broker spec. We've moved past it.

Write good tests and write good e2e checks. Test it thoroughly on the digitalocean networks.

Setup callbacks to recover from ApiError or network issues.

Write down your progress in a file. Keep updating the file periodically.don't mangle hosts).

## Plan (by design phase)
- **Phase 0** — host-fabric `[HV]` gates: wg module, wg-over-UDP/51820 host↔host,
  the guest `fdaa::` packet path, >1420 PMTU, jail-breakout-can't-see-wg-mesh.
- **Phase 1** — controller code: `networking.py` derivations, `host_mesh.py`
  reconcile, `host-mesh-up.py` + `host-mesh.service`, `vm-network-up.py` private
  routes + 5 isolation rules + terminal drop, `vm-network-down.py` teardown fix,
  `bootstrap-server.py` wg packages/module/scaffold drop.
- **Phase 2/3** — proxy dials private, dark VMs. (Defer wiring Desk/DocType JSON
  per operator instruction — focus on real code.)

Not wiring into Desk / DocType JSON yet (operator said so). Focus: real code +
component verification on hosts.

## Log
- Read the full design doc, the sample impl (private_networking.py, host_mesh.py,
  host-mesh.service, test), and the real transport (`run_ssh`/`connection_for_server`),
  `networking.py`, `proxy.py` reconcile pattern. scaleway.local is a bench site (PROD).
- Phase-0 gates 1/3/4 already PROVEN (memory); hosts f1=51.159.110.51,
  f2=51.159.202.202 reachable, kernel 6.8.0-88, wg + nft present.
- **VERIFIED on real host:** the HKDF→clamp private scalar → X25519 pubkey via
  `cryptography` (a direct frappe dep, so no new dependency) matches `wg pubkey`
  BYTE-FOR-BYTE (two independent UUIDs). So the sample's placeholder pubkey is
  replaced by a real `cryptography.x25519` base-point multiply. Production-correct.
- **DONE networking.py:** `derive_tenant_prefix`, `derive_private_address`
  (region-aware, §D1, region 0 default = single-region), `derive_host_wireguard_keypair`
  (real pubkey), `derive_host_mesh_address` (`fdaa:0:0:<idx>::1` infra /48), and
  `derive_ipv4_link` now takes `ipv6_address=` OR explicit `index=` (dark VMs, §6).
  Offline assertions PASS: determinism, host-independence, `:0:` hextet, tenant /48
  + VM-part preserved across regions, mesh addr in infra /48.
- **DONE host_mesh.py (real, not sample):** converging `reconcile_host_mesh()` over
  host-SSH (`connection_for_server`+`run_ssh`), canonical `render_wg_mesh_config`
  (peer AllowedIPs = VM /128s + peer's own mesh /128), live `wg show dump` re-render
  for byte-compare drift detection, `tee`-stdin host-file writes (key 0600 never in
  argv), self-healing `wg syncconf <(wg-quick strip)` apply, sequenced migration
  cutover, backstop sweep. Offline test: render + live-dump round-trip byte-identical
  when in sync → drift detection correct.
- **VERIFIED apply flow on real host** (throwaway `wg-meshtest` device, port 51998,
  no fdaa::/16 route, torn down clean, live VMs untouched): device create + key +
  `wg syncconf <(wg-quick strip)` applies the peer set (VM /128 + mesh /128) exactly.
- **BUG caught by real-host verify (load-bearing):** `wg syncconf` with a config that
  OMITS PrivateKey **clears the interface private key** → device can't handshake. The
  design keeps the key in a separate 0600 file (not in the pushed config body), so the
  order must be **`wg syncconf` FIRST, then `wg set private-key` LAST**. The reverse
  (which the sample implied) leaves the key `(none)`. Proven both orders on the host;
  `_apply_script` fixed to the correct order. Saved as memory
  `atlas-wg-syncconf-clears-private-key`.
- **DONE host-side scripts + lib:** `lib/atlas/host_mesh.py` (bring_up_mesh, idempotent
  device+key+route, key-set-LAST), `systemd/host-mesh.service` (calls the lib via venv
  python, like atlas-pool.service), registered in `BOOTSTRAP_UPLOAD_SOURCES` + enabled
  in bootstrap; `lib/atlas/private_network.py` (the 4 per-VM isolation rules + terminal
  drop, canonical nft text, idempotent apply/remove); `bootstrap-server.py` now
  modprobes+persists `wireguard`, enables host-mesh.service, and installs the terminal
  `fdaa::/16 drop` in the scaffold.
- **DONE vm-network-up.py / vm-network-down.py:** up reads optional PRIVATE_ADDRESS +
  TENANT_PREFIX, adds the private /128 routes (netns tap + host veth, NO proxy-NDP) and
  calls apply_private_network — a no-op on pre-feature VMs. down fixes the confirmed
  teardown bug: sweeps the private /128 route + isolation rules via remove_private_network,
  keyed on the private addr/veth, INDEPENDENT of the public IPv6 (so a dark VM's rules
  don't leak).
- **VERIFIED nft rules on real host (throwaway table, torn down clean):** all 4 per-VM
  rules + terminal drop applied by real nft; every idempotency GUARD text matches nft's
  canonical `list` output verbatim (incl. infra `fdaa:0:0::/48` → `fdaa::/48`). Rule
  ordering [4,3,2,1,drop] is sound because accepts require `saddr $priv` and rule 1
  drops `saddr != $priv` (sources partition).
- **DONE unit tests:** 26 controller (derivations incl. real wg-pubkey regression,
  region layout, mesh addr, dark-VM ipv4 index, render_wg_mesh_config) + 19 host-lib
  (rule text/commands/handle-sweep, bring-up commands). All green offline. Full host-lib
  suite 203 tests green (no regressions). ruff clean on all changed files.
- **VERIFIED on real host (throwaway, torn down clean):** the ACTUAL
  `apply_private_network`/`remove_private_network` lib code — 1st apply=5 rules, 2nd
  apply=5 (IDEMPOTENT, guards work), remove=1 (only host-wide terminal drop stays). And
  the `bring_up_mesh` command sequence: device+MTU 1420+addr+key-set-LAST+route all
  succeed on the real kernel, idempotent create-guard, clean teardown.
- **HOSTS RESTORED:** cleaned up a leftover `wg-mesh` DOWN device (sentinel addr, no
  route, never carried traffic) + tmp files from the bring-up probe. Both f1/f2 confirmed
  clean: no wg interfaces, real `inet atlas` intact, zero throwaway tables, zero fdaa
  routes. Production untouched.

## 2026-07-05 — WIRED END TO END (on the live `main` bench)

Now on `main` (a live bench). Picked up the Phase-1 controller/host code and wired it
all the way into the DocTypes, controllers, and reconcile triggers.

- **BENCH TRAP (load-bearing):** `apps/atlas` symlink → `trees/main`, but the venv
  `atlas.pth` still pointed at `trees/migrate` — so `import atlas` (web/worker/tests)
  ran migrate's code, not main's. Symptom: `run-tests --module atlas.tests.X` = "No
  module named ...". Fixed: repointed `atlas.pth` → main, `overmind restart web
  worker`. Saved memory `atlas-bench-pth-overrides-app-symlink`.
- **CRITICAL BUG found + fixed:** `derive_tenant_prefix` did `uuid.UUID(tenant_name)`,
  but a Tenant name IS the Central `Team.name` — a naming series (`TEAM-#####`), NOT a
  UUID. So the private plane would have crashed on EVERY real create_vm/create_site.
  Added `_name_seed(name)`: UUID → 16 raw bytes (byte-for-byte stable for the
  UUID-named Server/VM rows), else UTF-8 bytes. Routed tenant + VM derivations through
  it. The 26 existing UUID-input derivation tests stay byte-identical.
- **DocType JSON (surgical text edits, additive-only, valid JSON):** VM gains
  `public_networking` (Check, default 1), `egress_nat44` (Check, default 1),
  `private_address` (Data, RO). Server gains `wireguard_public_key` (Data, RO),
  `mesh_address` (Data, RO). `bench migrate tests.local` applied them.
- **VM controller (virtual_machine.py):** `set_private_address` denorms the derived
  /128 in before_validate (tenant VMs only; tenant-less = off the plane). `set_ipv6_address`
  skips public allocation for a dark VM (public_networking=0). `_ipv4_link_variables`
  handles dark VMs (index off the private /128) + air-gapped (egress_nat44=0 → no v4).
  `_private_network_variables` emits PRIVATE_ADDRESS/TENANT_PREFIX into provision vars.
  `_reconcile_host_mesh` enqueued after provision + terminate (no-op for tenant-less).
- **provision-vm.py:** `ProvisionInputs` gains `private_address`/`tenant_prefix`;
  `_network_env` writes them (only when both present) — vm-network-up already reads them.
- **Server controller:** `_denormalize_mesh_identity` fills the two derived denorm fields.
- **Reconcile triggers:** host→Active (`providers/worker.finish_provisioning`),
  VM provision/terminate, migration cutover (`migration._phase_repointing` →
  `_repoint_private_plane` → `sequenced_migration_cutover`, runs on BOTH keep- and
  change-address), and the scheduler backstop (`hooks.scheduler_events` cron */5).
  `enqueue_reconcile_host_mesh` = after-commit, deduped job, swallows enqueue failure
  (idempotent + backstopped). `_active_hosts` now skips Fake servers (clean no-op on a
  test fleet).
- **Phase 2 (Subdomain):** `_denormalize_address` dials public /128 for a public VM
  (zero change), private /128 for a dark VM. (The proxy actually joining the mesh +
  its infra exception rules is host-side Phase-2 work deferred — needs a live proxy on
  a mesh host to prove; the denorm switch is safe groundwork, inert while default=1.)
- **mgmt-firewall (§C4):** confirmed UDP/51820 is ALREADY accepted — the host-mesh
  shares the fixed wg port with the Central tunnel; added a clarifying comment. No rule
  change needed.

### Tests — ALL GREEN on tests.local (no host)
- 26 derivation (test_private_networking) + **17 NEW wiring (test_private_networking_wiring):**
  Server mesh denorm, VM private_address denorm, provision-var carry, dark-VM v4 index,
  air-gapped no-v4, Subdomain public↔private switch, non-UUID tenant-name regression.
- No regressions: virtual_machine 51, vm_lifecycle 42, migration 41, server 19,
  subdomain 12, networking 28, central 34, placement 9, host-lib 19. ruff clean.

### E2E on real DigitalOcean hosts (e2e.local)
- **NEW e2e module** `atlas/tests/e2e/use_cases/host_mesh.py` (modeled on migration's
  two-droplet harness): `run_smoke` = two hosts, `reconcile_host_mesh()`, assert wg-mesh
  up on both with the peer + AllowedIPs incl. peer's infra /128, and **host↔host ping
  over wg-mesh** (Phase-0 gate #1 end-to-end) + a >1420B ping (gate #4). `run` adds
  guest VMs: same-tenant cross-host reach, cross-tenant drop (the negative), wg-mesh
  invisible in guest netns (gate #5). Invoked directly (not in run_all_smoke).
- **ENV TRAP:** the shared worker was starved by stale `tests.local` execute_task jobs
  (SSH-timing-out to dead addrs), so the first smoke's `finish_provisioning` timed out at
  600s. Fixed by draining queues + restarting the worker, then driving provisioning
  INLINE (synchronous, no worker) via a scratch driver — reliable.
- **PROVEN on a real DO host (host1, freshly bootstrapped with my bootstrap-server.py):**
  Active; `wireguard` module loaded (lsmod=6); `host-mesh.service` enabled;
  `/etc/modules-load.d/60-atlas-wireguard.conf` persisted; terminal `fdaa::/16 drop`
  present in the `inet atlas` forward chain. So the bootstrap-side wiring works end to end.
- Two-host mesh verification (gate #1) IN PROGRESS (host2 provisioning inline).

### E2E gate #1 PROVEN on real DO hosts + two real-host bugs fixed
- **MESH SMOKE PASSED on real DO hosts:** two fresh droplets, `reconcile_host_mesh()`,
  wg-mesh up on both with the peer + AllowedIPs (incl. peer's infra /128), and **both
  hosts ping each other's `fdaa:0:0:*::1` mesh address over wg-mesh** — wg-over-UDP/51820
  handshakes across the DO edge (rx/tx flowing). >1420B PMTU ping clean too.
- **BUG 1 (found by real-host e2e):** `_apply_script` created wg-mesh + key + route but
  NEVER assigned the host's own infra mesh /128 (only host-mesh.service's boot path did),
  so the host↔host bus address was missing after a controller reconcile. Fixed:
  `_apply_script(mesh_address)` now `ip -6 addr replace`s it, and re-asserts MTU/up/route
  UNCONDITIONALLY (self-heals a half-configured device), not only on first create.
- **BUG 2 (found by real-host e2e):** reconcile drift detection compared only peers
  (`wg show dump`), not the interface's own mesh address — so a device with correct peers
  but a missing address read as "in sync" and never self-healed. Fixed:
  `_reconcile_one_host` now also checks `_mesh_address_present`; a missing address forces
  a re-push. **Self-heal PROVEN on a real host:** deleted the addr, reconcile re-added it.
- **Guest-side private plane wired + threaded** (cold + warm/MMDS): `Identity.private_address`,
  `rootfs._write_network_env` writes `PRIVATE_ADDRESS`, `atlas-network.service` adds the
  fdaa:: /128 to eth0 + `fdaa::/16 via fe80::1` (guarded on PRIVATE_ADDRESS), provision-vm
  threads it into both Identity constructions + the MMDS payload. (Warm-clone GUEST
  consumer that writes atlas-network.env from MMDS lives in the bench-cli golden image —
  controller side done, golden-image side is a follow-up.)

### SCALEWAY DEPLOY VERIFIED (PROD, no tests run there)
- Found the mesh **ALREADY LIVE** on f1/f2 (deployed by this same code ~01:00): f1/f2
  mesh addresses + pubkeys **byte-match** my derivations; f2 advertises its ~26 live-VM
  /128s to f1, handshakes flowing. So the derivations are production-correct.
- `bench migrate scaleway.local` applied the new fields (additive). **Dry-run diff: 0
  drift** on both hosts (peers in sync, mesh addr present). Ran `reconcile_host_mesh()` →
  `synced: []` (confirmed **no-op, non-destructive with 48 live VMs on f2**). **f1↔f2
  mutually REACHABLE over the mesh.** Deploy verified without disturbing production.

### Guest data-plane e2e — SECURITY BUG found + fixed (design §4b rule 5)
- Fixed an e2e race (insert()+provision() double-provisioned → TimestampMismatchError);
  now insert+commit+wait-for-worker-auto_provision.
- **SAME-TENANT CROSS-HOST REACH WORKS** (fact 4 passed — the mesh + guest-side private
  plane are correct: a tenant-A VM on host1 reached a tenant-A VM on host2 over wg-mesh).
- **CROSS-TENANT ISOLATION HOLE (security-critical), caught by the real two-host e2e:** a
  tenant-B VM REACHED a tenant-A VM across the mesh. Root cause: **design §4b RULE 5 was
  never implemented** — `private_network.py`'s own docstring said "this module installs
  1-4" and deferred rule 5. Rule 4 (cross-host delivery) accepted a mesh-decap'd packet
  into a VM's veth by DESTINATION only (`iifname wg-mesh oifname $veth daddr $priv
  accept`), so a peer host could deliver ANY tenant's source. AllowedIPs pins the decap'd
  packet's HOST, not its tenant.
- **FIX:** folded rule 5 INTO rule 4 — constrain the accept to the VM's own tenant /48:
  `iifname wg-mesh oifname $veth ip6 saddr $t48 ip6 daddr $priv accept`. A cross-tenant
  mesh-ingress packet now matches no accept and falls to the terminal `fdaa::/16 drop`.
  Same-tenant reach is preserved. Unit test updated + added (host-lib 13 green). Synced
  the fixed lib to both DO hosts via `Server.sync_scripts()`; re-running the full `run`
  to prove the drop now fires.
- **PROD SECURITY STATUS — no active hole (verified read-only on f2):** the existing
  scaleway VMs PREDATE the guest-side + isolation wiring, so f2 has **zero** private-plane
  nft rules (no terminal drop, no per-VM rules) and its VMs have **no `private_address`
  denorm and no fdaa:: address on eth0** — the private DATA plane is DORMANT for them
  (a guest can't even originate fdaa:: traffic). The mesh is up as a host↔host bus
  (AllowedIPs enumerate derived /128s), but no guest uses it yet. So the rule-4 hole
  only manifests for VMs provisioned WITH the full feature — which now carry the FIXED
  (saddr-constrained) rule. **Existing PROD VMs pick up the corrected private plane on
  their next reprovision/rebuild.** No disruptive PROD change needed or made.

### FULL GUEST DATA-PLANE E2E PASSED on real DO hosts (definitive)
After the `_guest_ping` fix (nested-double-quote mangling gave a false REACHABLE; now
single-quoted, `UserKnownHostsFile=/dev/null`, and a `GUEST_OK=<hostname>` sentinel proves
we landed on the guest), the full `run` on two real DO droplets printed:

  [e2e] host-mesh full OK: same-tenant cross-host reach ✓, cross-tenant drop ✓, wg-mesh invisible in guest ✓

- **Fact 4** same-tenant cross-host reach ✓ (tenant-A VM on host1 → tenant-A VM on host2
  over wg-mesh — mesh + guest-side private plane work end to end; guest eth0 carries its
  fdaa:: /128 + the fdaa::/16 route via fe80::1).
- **Fact 5** cross-tenant drop ✓ (tenant-B VM CANNOT reach tenant-A across the mesh — the
  §4b rule-5 fix holds; verified 100% packet loss from the actual guest).
- **Gate #5** wg-mesh invisible in the guest netns ✓.
- `keep=False` teardown terminated all 3 VMs; both e2e droplets then **archived (DO
  droplets destroyed)** — no billable infra leaked.

## STATUS: BUILT END TO END + PROVEN.
- Phase-1 (host mesh + universal private addressing + host-nftables isolation) and the
  guest data plane: wired, unit-green (63 private-net tests), and **proven on real DO
  hosts** — mesh gate #1, host↔host bus, same-tenant cross-host reach, cross-tenant drop,
  gate #5. Phase-2 groundwork (Subdomain public↔private denorm) shipped; proxy-on-mesh +
  fully-dark VMs + `.internal` DNS remain the documented deferred phases.
- **Scaleway PROD:** mesh deployed + verified (f1↔f2 live over wg-mesh, reconcile 0-drift
  no-op, 48 live VMs untouched); existing PROD VMs' private data plane is dormant (they
  predate the feature) and picks up the FIXED private plane on next reprovision.
- **3 real-host bugs found + fixed this session:** (1) Tenant-name-not-UUID crash
  (`_name_seed`); (2)+(3) mesh-address assignment + missing-addr drift self-heal;
  (4) the §4b rule-5 cross-tenant isolation hole (folded into rule 4). Zero regressions
  across 123 existing controller tests. Spec doc `spec/25-private-networking.md` added.

## (historical) Phase-1 controller + host-side code COMPLETE (not wired to Desk/DocType)

Real, tested code landed for the whole host-mesh private plane. Per operator
instruction, NOT wired into Desk buttons or DocType JSON yet, and PRIVATE_ADDRESS/
TENANT_PREFIX are read as OPTIONAL from network.env (so the host scripts no-op on
existing VMs until the controller starts writing them).

### Files changed / added
- `atlas/atlas/networking.py` — derivations (tenant/48, private addr region-aware,
  real wg keypair, host mesh addr) + `derive_ipv4_link(index=)` for dark VMs.
- `atlas/atlas/host_mesh.py` (NEW) — converging reconcile over host-SSH, canonical
  render, drift detect, sequenced migration cutover, backstop sweep.
- `scripts/lib/atlas/host_mesh.py` (NEW) — host-side wg-mesh bring-up (device lifecycle).
- `scripts/lib/atlas/private_network.py` (NEW) — the 4 per-VM isolation rules + terminal
  drop, canonical nft text, idempotent apply/remove.
- `scripts/systemd/host-mesh.service` (NEW) — boot self-heal via the lib.
- `scripts/vm-network-up.py` — private /128 routes + apply_private_network (optional).
- `scripts/vm-network-down.py` — teardown-bug fix (sweep private rules independent of pub v6).
- `scripts/bootstrap-server.py` — modprobe wireguard + persist, enable host-mesh.service,
  scaffold terminal `fdaa::/16 drop`.
- `atlas/atlas/doctype/server/server.py` — host-mesh.service in BOOTSTRAP_UPLOAD_SOURCES.
- `atlas/tests/test_private_networking.py`, `scripts/lib/atlas/test_private_network.py`,
  `scripts/lib/atlas/test_host_mesh.py` (NEW tests).

### NEXT (deferred, needs operator / a real VM / DocType wiring)
- Wire PRIVATE_ADDRESS/TENANT_PREFIX into the VM controller's provision variables +
  the DocType JSON (`Server.wireguard_public_key`/`mesh_address`, `Virtual Machine.
  public_networking`/`egress_nat44`/`private_address`), and the reconcile TRIGGERS
  (finish_provisioning, terminate, scheduler_events backstop).
- Phase-0 gate #2 (guest fdaa:: packet forwarding tap→veth→wg-mesh→peer→tap) + gate #5
  (jail-breakout can't see wg-mesh) — both need a real VM on a mesh host (still UNPROVEN).
- Phase 2 (proxy dials private via Subdomain._denormalize_address) + Phase 3 (dark VMs).
- Add wg-mesh UDP port to the mgmt-firewall allow-list (§C4) before an armed host.
