// Unit tests for derive.js — the load-bearing join. Run with plain node (no test
// framework dependency, matching the app's zero-runtime-deps discipline):
//
//   node src/derive.test.mjs
//
// The mock fixtures all share ONE shape (the v2/current shape backend/server.py
// emits) and differ only in cardinality:
//   · state-ordinary.json — ~24 VMs, generated (deterministic uuids, enriched
//     provisioning fields, Scaleway no-anchor reserved IP).
//   · state-scale.json    — ~1000 VMs, generated (some Failed), same shape.
//   · state-real.json     — a real host capture (f2-aditya-blr3), which the real
//     backend produces: host_veth-keyed nft, `dnat ip to`, anchor null, and NO
//     enriched provisioning fields (so provisioning must degrade, not throw).
//
// The assertions prefer STRUCTURAL INVARIANTS (the reserved VM gets an In-v4
// DNAT leg, operator VM is the operator tenant, shared+dedicated == committed,
// alertGroups folds scale to ≤6, …) over magic uuids/counts, so the suite
// survives fixture regeneration. Where a specific VM is needed it's looked up by
// a stable property (reserved_ipv4, role, provisioning, state), never a uuid
// prefix.

import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  deriveVm,
  derivePath,
  deriveFilterSentence,
  vmByRule,
  hostResidue,
  quotas,
  alerts,
  alertGroups,
  alertCounts,
  events,
  sizeDistribution,
  vmIngress,
  vmPrivateMesh,
  vmMigrating,
  vmTenant,
  isOperator,
  tenantSummary,
  tenantFacets,
  diskOrigin,
  provisioning,
  perVmProvisioning,
} from "./derive.js";

const here = dirname(fileURLToPath(import.meta.url));
const load = (f) =>
  JSON.parse(readFileSync(resolve(here, "../mock", f), "utf-8"));

let pass = 0;
let fail = 0;
function ok(cond, msg) {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error("  ✗ " + msg);
  }
}
function eq(a, b, msg) {
  ok(
    JSON.stringify(a) === JSON.stringify(b),
    `${msg} — got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`
  );
}

// ── mock/state-ordinary.json — the small generated fixture ───────────────────
{
  const state = load("state-ordinary.json");
  const vms = state.virtual_machines;

  // The reserved-IP VM: In-v4 DNAT leg (NO anchor hop — Scaleway no-anchor),
  // Out-v4 SNAT back to the reserved IP, joined ndp/unit, both nat rules.
  {
    const vm = vms.find((v) => v.reserved_ipv4);
    ok(vm, "ordinary: a reserved-IP VM exists");
    const d = deriveVm(state, vm);
    const path = derivePath(d);

    const inV4 = path.find((l) => l.dir === "In v4");
    ok(inV4, "reserved: has an In v4 leg");
    eq(
      [inV4.from, inV4.to],
      [vm.reserved_ipv4, vm.ipv4_guest],
      "reserved: In-v4 is reserved → guest"
    );
    ok(
      inV4.hop === undefined,
      "reserved: no anchor hop (Scaleway no-anchor shape)"
    );
    eq(inV4.xf, "DNAT", "reserved: In-v4 transform is DNAT");
    ok(inV4.key === true, "reserved: reserved IP is the key (darkest) node");

    const outV4 = path.find((l) => l.dir === "Out v4");
    ok(outV4 && outV4.xf === "SNAT", "reserved: Out-v4 leg is SNAT");
    eq(
      [outV4.from, outV4.to],
      [vm.ipv4_guest, vm.reserved_ipv4],
      "reserved: Out-v4 guest → reserved"
    );

    ok(
      path.some((l) => l.dir === "In v6"),
      "reserved: also has the routed /128 In-v6 leg"
    );

    ok(d.dnat && d.snat, "reserved: found both DNAT and SNAT rules");
    ok(d.dnat.includes(vm.ipv4_guest), "reserved: DNAT targets the guest v4");
    ok(
      d.snat.includes(vm.reserved_ipv4),
      "reserved: SNAT rewrites to the reserved v4"
    );
    ok(
      d.unit && d.unit.active === "active" && d.unit.sub === "running",
      "reserved: unit active·running"
    );
    ok(
      d.ndp && d.ndp.address === vm.ipv6,
      "reserved: joined its proxy-NDP entry"
    );
    ok(
      d.filterRules.length >= 2,
      "reserved: joined its forward-chain filter rules"
    );
  }

  // A non-reserved VM published through the proxy: In-v6 + Out-v4 masquerade,
  // NO In-v4 leg.
  {
    const proxied = (state.proxy_maps || [])[0];
    ok(proxied, "ordinary: proxy_maps present");
    const vm = vms.find((v) => v.uuid === proxied.vm);
    ok(vm && !vm.reserved_ipv4, "ordinary: proxied VM has no reserved IP");
    const d = deriveVm(state, vm);
    const path = derivePath(d);
    ok(
      path.some((l) => l.dir === "In v6"),
      "proxied: has In v6 leg"
    );
    ok(
      !path.some((l) => l.dir === "In v4"),
      "proxied: has NO In v4 leg (no reserved)"
    );
    const out = path.find((l) => l.dir === "Out v4");
    ok(out && out.xf === "masquerade", "proxied: Out-v4 is masquerade");
    eq(out.from, vm.ipv4_guest, "proxied: masquerade from guest v4");
  }

  // A stopped VM: path is null, unit inactive·dead.
  {
    const vm = vms.find((v) => v.state === "Stopped");
    ok(vm, "ordinary: a Stopped VM exists");
    const d = deriveVm(state, vm);
    ok(derivePath(d) === null, "stopped: path is null (live rows torn down)");
    ok(
      d.unit && d.unit.active === "inactive" && d.unit.sub === "dead",
      "stopped: unit inactive·dead"
    );
  }

  // vmByRule maps the reserved VM's DNAT rule back to it; host fabric → null.
  {
    const idx = vmByRule(state);
    const rv = vms.find((v) => v.reserved_ipv4);
    const d = deriveVm(state, rv);
    ok(
      idx.rule(d.dnat) === rv.uuid,
      "vmByRule: DNAT rule maps back to the reserved VM"
    );
    ok(
      idx.rule("ip daddr 169.254.169.254 drop") === null,
      "vmByRule: metadata drop is host fabric"
    );
    // The masquerade-all rule (guest CIDR → uplink) belongs to no single VM.
    const nft = (state.nft_tables || []).flatMap((t) =>
      (t.chains || []).flatMap((c) => c.rules || [])
    );
    const masqAll = nft.find(
      (r) => /masquerade/.test(r) && /100\.64\.0\.0\//.test(r)
    );
    ok(masqAll, "ordinary: a masquerade-all host rule exists");
    ok(idx.rule(masqAll) === null, "vmByRule: masquerade-all is host fabric");
  }

  // hostResidue keeps the default routes, the metadata-drop rule and the host
  // service units out of any VM.
  {
    const residue = hostResidue(state);
    ok(
      residue.routes.some((r) => r.dest === "default"),
      "hostResidue: default routes are host fabric"
    );
    ok(
      residue.nftRules.some((n) => /169\.254\.169\.254/.test(n.rule)),
      "hostResidue: metadata-drop rule is host fabric"
    );
    ok(
      residue.units.some((u) => u.name === "atlas-pool.service"),
      "hostResidue: atlas-pool.service is host fabric"
    );
    // No VM row leaks in: every residue nft rule is unowned by construction.
    const idx = vmByRule(state);
    ok(
      residue.nftRules.every((n) => idx.rule(n.rule) === null),
      "hostResidue: residue is genuinely unclaimed"
    );
  }

  // filter sentence mentions both directions + the metadata block.
  {
    const rv = vms.find((v) => v.reserved_ipv4);
    const d = deriveVm(state, rv);
    const sentence = deriveFilterSentence(state, d);
    ok(
      /allowed both ways/.test(sentence),
      "filter sentence: veth allowed both ways"
    );
    ok(
      /169\.254\.169\.254 blocked/.test(sentence),
      "filter sentence: metadata blocked"
    );
  }

  // Derive EVERY VM — must not throw; every running VM yields a path, every
  // stopped one yields null.
  {
    let threw = 0;
    let runningPathed = true;
    let stoppedNull = true;
    for (const vm of vms) {
      try {
        const d = deriveVm(state, vm);
        const p = derivePath(d);
        if (vm.state === "Running" && !p) runningPathed = false;
        if (vm.state === "Stopped" && p !== null) stoppedNull = false;
      } catch {
        threw++;
      }
    }
    ok(threw === 0, "ordinary: derived every VM without throwing");
    ok(runningPathed, "ordinary: every running VM produced a path");
    ok(stoppedNull, "ordinary: every stopped VM produced a null path");
  }
}

// ── the status/scale layer (Overview domain) against state-ordinary.json ─────
{
  const state = load("state-ordinary.json");
  const vms = state.virtual_machines;
  const host = state.host;

  // quotas() — committed vs the over-provision budget (physical × factor).
  const q = quotas(state);
  const cpu = q.find((b) => b.label === "vCPU");
  const mem = q.find((b) => b.label === "Memory");
  const pool = q.find((b) => b.label === "Pool");
  ok(cpu && mem && pool, "quotas: three bars (vCPU, Memory, Pool)");

  // Budget = physical × overprovision_factor, read from the host, not hard-coded.
  eq(
    cpu.budget,
    host.cpu_total * host.overprovision_factor,
    "quotas: vCPU budget = cpu_total × factor"
  );
  eq(cpu.physical, host.cpu_total, "quotas: vCPU physical = host cpu_total");
  eq(
    mem.budget,
    host.mem_total_mib * host.overprovision_factor,
    "quotas: Memory budget = mem_total × factor"
  );
  // used = Σ running, idle = Σ stopped/paused — sum invariant against the VMs.
  const run = vms.filter((v) => v.state === "Running");
  const idle = vms.filter((v) => v.state === "Stopped" || v.state === "Paused");
  eq(
    cpu.used,
    run.reduce((n, v) => n + (v.vcpus || 0), 0),
    "quotas: vCPU used = Σ running vcpus"
  );
  eq(
    cpu.idle,
    idle.reduce((n, v) => n + (v.vcpus || 0), 0),
    "quotas: vCPU idle = Σ stopped vcpus"
  );

  // Pool reads its % straight from lvs and resolves absolute GiB from the size.
  eq(pool.used, state.pool.data_percent, "quotas: Pool reads lvs data_percent");
  ok(
    Math.abs(pool.totalGib - 686.16) < 0.01,
    "quotas: Pool total GiB parsed from lvs size"
  );
  ok(
    Math.abs(pool.usedGib - pool.totalGib * (pool.used / 100)) < 0.01,
    "quotas: Pool used GiB = total × fraction"
  );
  // Severity is derived (not hard-coded) — it must match its own fraction band.
  const band = (f) =>
    f == null ? "ok" : f >= 0.9 ? "crit" : f >= 0.75 ? "warn" : "ok";
  ok(
    q.every((b) => b.severity === band(b.fraction)),
    "quotas: severity follows the fraction band"
  );

  // alerts() — every firing alert traces to a true fact; keys are stable.
  const model = alerts(state);
  ok(model.firing.length > 0, "alerts: ordinary fixture fires something");
  ok(
    model.cleared.length === 0,
    "alerts: no history yet → cleared empty (honest)"
  );
  ok(
    model.firing.every((a) => typeof a.key === "string" && a.key.includes(":")),
    "alerts: every alert carries a stable kind:id key"
  );
  // A disk-hot VM (≥85%) fires a disk warn keyed on its uuid.
  const hot = vms.find(
    (v) =>
      v.disk_data_percent != null &&
      v.disk_data_percent >= 85 &&
      v.disk_data_percent < 95
  );
  if (hot) {
    const a = model.firing.find((a) => a.key === `disk:${hot.uuid}`);
    ok(
      a && a.severity === "warn",
      "alerts: disk-hot VM fires a warn keyed on its uuid"
    );
  }
  // An idle-but-reserved (stopped, still holding capacity) VM fires.
  const stoppedHolding = vms.find(
    (v) => v.state === "Stopped" && (v.vcpus || v.mem_mib)
  );
  if (stoppedHolding) {
    ok(
      model.firing.some((a) => a.key === `idle:${stoppedHolding.uuid}`),
      "alerts: idle-but-reserved stopped VM fires"
    );
  }
  // alertCounts sums crit/warn to the firing length (no other severities).
  const counts = alertCounts(state);
  eq(
    counts.crit + counts.warn,
    model.firing.length,
    "alertCounts: crit+warn == firing count"
  );

  // alertGroups() folds firing by kind — each group carries a worst-severity +
  // count, and a singular group carries its VM for a direct jump.
  const groups = alertGroups(state);
  ok(groups.length <= 6, "alertGroups: ordinary folds to a small landing");
  eq(
    groups.reduce((n, g) => n + g.count, 0),
    model.firing.length,
    "alertGroups: group counts sum back to firing count"
  );
  // A singular group of a per-VM kind (disk/failed/idle) carries its VM for a
  // direct jump; host-wide pressure alerts have no VM, so they're exempt.
  ok(
    groups.every((g) => g.count !== 1 || g.key === "pressure" || g.vm),
    "alertGroups: singular per-VM groups carry a VM jump"
  );

  // sizeDistribution() — running VMs binned by power-of-two RANGE; counts sum to
  // the running total, and interior empty ranges are kept for a continuous shape.
  const dist = sizeDistribution(state);
  eq(dist.total, run.length, "dist: total = running VM count");
  eq(
    dist.buckets.reduce((n, b) => n + b.count, 0),
    run.length,
    "dist: bucket counts sum to running total"
  );
  ok(dist.buckets.length >= 1, "dist: at least one occupied bucket");
  ok(
    dist.buckets[0].count > 0 &&
      dist.buckets[dist.buckets.length - 1].count > 0,
    "dist: span trimmed to occupied"
  );
  // A bucket's weight is the summed resource in that range; `max` scales the bars.
  eq(
    Math.round(dist.buckets.reduce((n, b) => n + b.weight, 0)),
    Math.round(run.reduce((n, v) => n + (v.mem_mib || 0) / 1024, 0)),
    "dist: RAM weights sum to total running mem (GiB)"
  );
  eq(
    dist.max,
    Math.max(...dist.buckets.map((b) => b.weight)),
    "dist: max is over weight"
  );
  eq(dist.unit, "GiB", "dist: RAM unit is GiB");

  // The resource switch re-bins the same running set by CPU / disk. Counts still
  // sum to the running total; CPU weight is the summed vCPU.
  const cpuDist = sizeDistribution(state, "cpu");
  eq(cpuDist.unit, "vCPU", "dist: CPU unit is vCPU");
  eq(
    cpuDist.buckets.reduce((n, b) => n + b.count, 0),
    run.length,
    "dist: CPU bucket counts sum to running total"
  );
  eq(
    cpuDist.buckets.reduce((n, b) => n + b.weight, 0),
    run.reduce((n, v) => n + (v.vcpus || 0), 0),
    "dist: CPU weights sum to total running vCPU"
  );

  // events() — tasks + migrations folded, migration present.
  const ev = events(state);
  ok(
    ev.length >= (state.tasks || []).length,
    "events: at least the tasks are folded in"
  );
  ok(
    ev.some((e) => e.kind === "migration"),
    "events: migration present"
  );

  // per-VM facets (Plan A). Look VMs up by stable property, not uuid.
  const reserved = vms.find((v) => v.reserved_ipv4);
  const proxied = vms.find(
    (v) =>
      (state.proxy_maps || []).some((m) => m.vm === v.uuid) && !v.reserved_ipv4
  );
  eq(
    vmIngress(state, reserved).kind,
    "reserved",
    "facet: reserved VM → reserved ingress"
  );
  eq(
    vmIngress(state, proxied).kind,
    "proxy",
    "facet: proxied VM → proxy ingress"
  );
  // Private mesh strips the CIDR off the VM's /128.
  if (reserved.private_ipv6) {
    eq(
      vmPrivateMesh(state, reserved),
      reserved.private_ipv6.split("/")[0],
      "facet: private mesh /128 stripped"
    );
  }
  const migVm = vms.find((v) => v.migrating);
  if (migVm)
    ok(vmMigrating(state, migVm) === true, "facet: migrating VM flagged");
  const stillVm = vms.find((v) => !v.migrating);
  if (stillVm)
    ok(
      vmMigrating(state, stillVm) === false,
      "facet: non-migrating VM not flagged"
    );

  // Tenancy — the operator VMs are the operator tenant; customers are counted apart.
  const op = vms.find((v) => v.role === "reverse-proxy");
  ok(op, "tenant: a reverse-proxy operator VM exists");
  eq(vmTenant(op), "operator", "tenant: proxy VM is the operator tenant");
  ok(isOperator(op), "tenant: isOperator true for the proxy VM");
  ok(
    !isOperator(reserved) || reserved.tenant === "operator",
    "tenant: customer VM is not operator"
  );
  // diskOrigin of an operator VM reads its role when it has no image origin;
  // otherwise it's simply a non-empty label (never a fabricated "image …").
  ok(
    diskOrigin(op) && diskOrigin(op) !== "—",
    "tenant: operator VM has a legible disk origin"
  );
  const ts = tenantSummary(vms);
  ok(ts.includes("operator"), "tenant: summary names the operator split");
  const facets = tenantFacets(vms);
  eq(facets[0].id, "operator", "tenant: facets put operator first");
  ok(
    facets.every((f) => f.count > 0),
    "tenant: every facet has a count"
  );
  eq(
    facets.reduce((n, f) => n + f.count, 0),
    vms.length,
    "tenant: facet counts cover every VM"
  );
}

// ── provisioning(state) + perVmProvisioning(vm) — the enriched fields ─────────
// Generated fixtures carry the provisioning fields; assert the commit/use math
// as invariants against the fixture's own VMs.
{
  const state = load("state-ordinary.json");
  const vms = state.virtual_machines;
  const running = vms.filter((v) => v.state === "Running");

  const p = provisioning(state);
  const cpu = p.resources.find((r) => r.label === "CPU");
  const mem = p.resources.find((r) => r.label === "Memory");
  const disk = p.resources.find((r) => r.label === "Disk");
  ok(cpu && mem && disk, "provisioning: three resources (CPU/Memory/Disk)");

  // CPU committed = Σ request_cores across running; physical = host cpu_total.
  const cpuCommitted = running.reduce(
    (n, v) => n + (v.cpu_request_cores ?? v.vcpus ?? 0),
    0
  );
  ok(
    Math.abs(cpu.committed - cpuCommitted) < 1e-9,
    "provisioning: CPU committed = Σ running requests"
  );
  eq(
    cpu.physical,
    state.host.cpu_total,
    "provisioning: CPU physical = host cpu_total"
  );
  ok(
    Math.abs(cpu.overcommit - cpu.committed / cpu.physical) < 1e-9,
    "provisioning: CPU overcommit = committed/physical"
  );
  ok(
    cpu.committed > cpu.physical,
    "provisioning: CPU is overcommitted (committed past physical)"
  );
  // used is real consumption, well under physical → the overcommit bet pays off.
  ok(
    cpu.used < cpu.physical,
    "provisioning: CPU used under physical (headroom real)"
  );
  ok(cpu.usedFrac < 1, "provisioning: CPU usage fraction < 1 (bar never full)");
  // Shared + dedicated committed must sum back to the total committed.
  ok(
    Math.abs(cpu.sharedCommitted + cpu.dedicatedCommitted - cpu.committed) <
      1e-9,
    "provisioning: CPU shared+dedicated committed = total committed"
  );
  // Severity reads off real usage, not paper overcommit.
  ok(
    cpu.severity === "ok",
    "provisioning: CPU severity from real usage, not paper overcommit"
  );

  // Memory cap == request → committed = Σ mem_request (running).
  const memCommitted = running.reduce(
    (n, v) => n + (v.mem_request_mib ?? v.mem_mib ?? 0),
    0
  );
  eq(
    mem.committed,
    memCommitted,
    "provisioning: Memory committed = Σ mem_request (running)"
  );
  eq(
    mem.physical,
    state.host.mem_total_mib,
    "provisioning: Memory physical = host mem_total_mib"
  );
  ok(
    Math.abs(mem.sharedCommitted + mem.dedicatedCommitted - mem.committed) <
      1e-9,
    "provisioning: Memory shared+dedicated == committed"
  );

  // Disk committed = Σ allocated (all VMs); physical = pool size in GiB.
  ok(
    disk.physical > 680 && disk.physical < 690,
    "provisioning: Disk physical ≈ pool size (686 GiB)"
  );
  ok(
    disk.committedFrac < 1,
    "provisioning: Disk commitment fits inside pool (×<1)"
  );

  // counts: shared/dedicated partition the running VMs.
  eq(
    p.counts.running,
    running.length,
    "provisioning: running count = Σ running VMs"
  );
  eq(
    p.counts.shared,
    running.filter((v) => v.provisioning === "shared").length,
    "provisioning: shared count"
  );
  eq(
    p.counts.dedicated,
    running.filter((v) => v.provisioning === "dedicated").length,
    "provisioning: dedicated count"
  );
  ok(
    p.counts.shared + p.counts.dedicated <= p.counts.running,
    "provisioning: shared+dedicated ⊆ running"
  );

  // perVmProvisioning — a dedicated VM (exact) vs a shared VM (overprovision).
  const ded = perVmProvisioning(
    vms.find((v) => v.provisioning === "dedicated")
  );
  const sh = perVmProvisioning(vms.find((v) => v.provisioning === "shared"));
  eq(ded.kind, "dedicated", "perVm: dedicated kind surfaced");
  eq(ded.shared, false, "perVm: dedicated not shared");
  eq(sh.kind, "shared", "perVm: shared kind surfaced");
  eq(sh.shared, true, "perVm: shared flagged shared");
  ok(
    sh.cpu !== "" && sh.mem !== "",
    "perVm: shared VM has cpu+mem provisioned"
  );
  ok(
    sh.memUsedFrac != null && sh.memUsedFrac >= 0,
    "perVm: mem usage fraction resolved"
  );
  // A VM with no provisioning fields degrades to blanks, doesn't throw.
  const bare = perVmProvisioning({ uuid: "x" });
  eq(bare.kind, null, "perVm: missing fields → null kind, no throw");
  eq(bare.cpu, "", "perVm: missing fields → blank cpu");
}

// ── state-real.json — the real host_veth / "dnat ip to" / no-anchor shape ─────
// The real backend never emits the enriched provisioning fields, so this fixture
// also proves provisioning()/perVmProvisioning() degrade gracefully.
{
  const state = load("state-real.json");
  const vms = state.virtual_machines;

  // A running, non-reserved VM: forward rules join via host_veth; In-v6 + Out-v4
  // masquerade; real disk origin + data% + size read off the raw fields.
  {
    const vm = vms.find((v) => v.state === "Running" && !v.reserved_ipv4);
    ok(
      vm && vm.host_veth,
      "real: a running non-reserved VM with a host_veth exists"
    );
    const d = deriveVm(state, vm);
    ok(d.filterRules.length >= 2, "real: joined forward rules via host_veth");
    ok(
      d.diskOrigin && d.diskOrigin !== "—",
      "real: disk origin reads from lvs origin"
    );
    eq(
      d.dataPercent,
      vm.disk_data_percent,
      "real: disk data% joined off the VM row"
    );
    eq(
      d.size,
      `${vm.vcpus} · ${vm.mem_mib}m`,
      "real: vCPU·mem size from firecracker fields"
    );

    const path = derivePath(d);
    ok(
      path.some((l) => l.dir === "In v6"),
      "real: In v6 leg present"
    );
    ok(
      path.some((l) => l.dir === "Out v4" && l.xf === "masquerade"),
      "real: Out-v4 masquerade (no reserved)"
    );
    ok(
      !path.some((l) => l.dir === "In v4"),
      "real: no In-v4 leg for a non-reserved VM"
    );
  }

  // The real reserved-IP VM gets an In-v4 DNAT leg with NO anchor hop.
  {
    const ri = (state.reserved_ips || [])[0];
    ok(ri, "real: a reserved_ips row exists");
    ok(
      ri.anchor == null,
      "real: reserved row has anchor null (Scaleway no-anchor)"
    );
    const vm = vms.find(
      (v) => v.uuid === ri.attached_vm && v.state === "Running"
    );
    ok(vm && vm.reserved_ipv4, "real: reserved IP is attached to a running VM");
    const path = derivePath(deriveVm(state, vm));
    const inV4 = path.find((l) => l.dir === "In v4");
    ok(inV4, "real: reserved VM gets an In-v4 leg");
    ok(inV4.hop === undefined, "real: Scaleway In-v4 leg has NO anchor hop");
    eq(
      [inV4.from, inV4.to],
      [vm.reserved_ipv4, vm.ipv4_guest],
      "real: In-v4 reserved → guest direct"
    );
  }

  // Derive EVERY real VM without throwing (the host_veth / IPv6-daddr matchers
  // must survive the real shape).
  {
    let threw = 0;
    for (const vm of vms) {
      try {
        derivePath(deriveVm(state, vm));
      } catch {
        threw++;
      }
    }
    ok(threw === 0, "real: derived every VM without throwing");
  }

  // provisioning() degrades: no enriched fields → falls back to vcpus/mem_mib for
  // committed, shared/dedicated counts are 0, disk physical fails the odd "<886.24g"
  // size to null — all without throwing.
  {
    let threw = false;
    let p;
    try {
      p = provisioning(state);
    } catch {
      threw = true;
    }
    ok(!threw, "real: provisioning() does not throw without enriched fields");
    ok(
      p && p.resources.length === 3,
      "real: provisioning() still returns three resources"
    );
    eq(p.counts.shared, 0, "real: no shared VMs (no provisioning field)");
    eq(p.counts.dedicated, 0, "real: no dedicated VMs (no provisioning field)");
    eq(
      p.counts.running,
      vms.filter((v) => v.state === "Running").length,
      "real: running count intact"
    );
    // perVmProvisioning on a real VM (no fields) → null kind, no throw.
    let pvThrew = false;
    let pv;
    try {
      pv = perVmProvisioning(vms[0]);
    } catch {
      pvThrew = true;
    }
    ok(!pvThrew, "real: perVmProvisioning() does not throw");
    eq(pv.kind, null, "real: perVm kind null (no provisioning field)");
  }
}

// ── state-scale.json — ~1000 VMs: the join stays sane, alertGroups stays small ─
{
  const state = load("state-scale.json");
  const vms = state.virtual_machines;
  ok(vms.length >= 1000, "scale: ~1000 VMs (customer fleet + operator VMs)");

  // Derive every VM — must not throw; each running VM yields a non-null path.
  let derived = 0;
  let pathed = 0;
  let threw = 0;
  for (const vm of vms) {
    try {
      const d = deriveVm(state, vm);
      derived++;
      if (derivePath(d)) pathed++;
    } catch {
      threw++;
    }
  }
  ok(threw === 0, "scale: derived every VM without throwing");
  ok(derived === vms.length, "scale: derived every VM");
  ok(pathed > 0, "scale: some VMs produced a path");

  // A reserved VM gets an In-v4 DNAT leg with NO anchor hop (no-anchor shape).
  const rv = vms.find((v) => v.reserved_ipv4);
  if (rv) {
    const inV4 = derivePath(deriveVm(state, rv)).find((l) => l.dir === "In v4");
    ok(
      inV4 && inV4.hop === undefined,
      "scale: reserved VM gets a DNAT leg with no anchor hop"
    );
  }

  // A Failed VM fires a critical alert; crit sorts before warn.
  const model = alerts(state);
  ok(
    model.firing.some((a) => a.severity === "crit"),
    "alerts: scale fixture fires a crit"
  );
  ok(
    model.firing.some((a) => a.key.startsWith("failed:")),
    "alerts: failed-VM alert present"
  );
  const firstWarn = model.firing.findIndex((a) => a.severity === "warn");
  const lastCrit = model.firing.map((a) => a.severity).lastIndexOf("crit");
  ok(
    firstWarn === -1 || lastCrit < firstWarn,
    "alerts: crit sorted before warn"
  );

  // quotas() must not throw on 1000 VMs and stays finite; the over-provisioned
  // vCPU is crit.
  const q = quotas(state);
  ok(
    q.every((b) => b.fraction == null || b.fraction >= 0),
    "quotas: scale fixture sane"
  );
  ok(
    q.find((b) => b.label === "vCPU").severity === "crit",
    "quotas: scale over-budget vCPU is crit"
  );

  // alertGroups() stays SMALL even with hundreds firing — the landing must be
  // constant-size (folded by kind, not per-VM). This is the load-bearing check.
  const groups = alertGroups(state);
  ok(
    groups.length <= 6,
    `alertGroups: folds hundreds into ≤6 lines (got ${groups.length})`
  );
  ok(
    model.firing.length > groups.length * 5,
    "alertGroups: many alerts, few groups (real folding)"
  );
  eq(
    groups.reduce((n, g) => n + g.count, 0),
    model.firing.length,
    "alertGroups: group counts still sum to firing count at scale"
  );
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
