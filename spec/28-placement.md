# 28 — Placement: load-aware host selection for the size ladder

When a user creates a Virtual Machine they never pick a host — the controller
fills `server` (spec/11). This document specifies how it picks, why the pick is
load-aware without being a scheduler, and the capacity accounting and safety gates
that back it. It is the source of truth for `atlas/atlas/placement.py`,
`atlas/atlas/packing.py`, `atlas/atlas/api/server_capacity.py`, and the offline
simulator (`atlas/atlas/packing_sim.py`).

## The one insight: the ladder is proportional

Every size preset (spec/`sizes.py`) is an exact scalar multiple of one **share
unit** — a Shared 1x — on the three packed axes:

| Preset       | units | cpu_max_cores | memory MB | disk GB |
| ------------ | ----- | ------------- | --------- | ------- |
| Shared 1x    | 1     | 0.0625        | 512       | 10      |
| Shared 2x    | 2     | 0.125         | 1024      | 20      |
| Shared 4x    | 4     | 0.25          | 2048      | 40      |
| Shared 8x    | 8     | 0.5           | 4096      | 80      |
| Dedicated 1x | 16    | 1             | 8192      | 160     |

`SHARE_UNIT` is derived from `SIZE_PRESETS["Shared 1x"]`, and
`test_sizes.test_proportionality_invariant` pins that every preset is the **same**
integer multiple of it on all three axes. Consequences (the design, not trivia):

1. **Packing is one-dimensional.** A host holds
   `U = min(cpu/0.0625, memory/512, disk/10)` share units, and *any* mix of preset
   VMs whose unit-sum ≤ U fits. There is no intra-host, cross-axis fragmentation, so
   no bin-packing solver is needed or wanted.
2. **The CPU axis is the guarantee, not the ceiling.** `cpu_max_cores` is the
   guaranteed share — the cgroup `cpu.weight` floor under contention in the default
   `CPU_MODE_RELAXED` (spec/networking). A shared VM bursts to a full core when the
   host is idle (`cpu.max = vcpus × period`); `CPU_MODE_HARD` (Dedicated) makes the
   share a wall. So packing on `cpu_max_cores` is packing on guarantees — the burst
   ceiling is a separate cgroup concern, not a packing dimension.
3. **Waste is a host-shape problem.** Whichever axis is the `min` in `U` strands the
   others. The ladder's balanced shape is **8 GB RAM per guaranteed core** and
   **20 GB pool disk per GB RAM**. On a typical 2–4 GB/core cloud host RAM binds and
   CPU is only 25–50% subscribed at full RAM. The fleet is shape-undecided, so the
   deliverable is *visibility*: per-host share units and per-axis stranding in Desk
   (below) so the operator can compare shapes.

## Capacity accounting (`api/server_capacity.py`)

Per host, per axis (`cpu` / `memory` / `disk`): `{total, effective, used}`.

- `total` — the host's physical amount, stamped at bootstrap (below). `None` =
  uncatalogued → the axis is **unlimited** (the operator vouched for the host by
  Activating it).
- `effective` — the budget placement checks against: CPU `total × overprovision_factor`;
  memory `total − host_memory_reserve_megabytes` (clamped ≥ 0); disk `total`.
- `used` — the sum over non-Terminated VMs: CPU by `cpu_max_cores` (bandwidth, not
  thread count), memory by `memory_megabytes`, disk by `disk_gigabytes + data_disk`.

**Memory floor.** `Atlas Settings.host_memory_reserve_megabytes` (default 1024) is
carved off the memory budget: guest RAM must never pack to 100% of MemTotal — the
host OS, per-VM Firecracker/jailer overhead, and thin-pool metadata share that RAM,
and packing to MemTotal OOMs the host.

**Share units + stranded (reporting only).** When at least one axis is measured,
`capacity_for_server` also returns `share_units {total, used, free}` and `stranded`
(per axis, `effective − total × unit_cost`). `cluster_capacity` rolls them up over
measured hosts. The Server form shows a one-line headline
("Capacity — CPU 12% · RAM 45% · disk 20% · 30/34 units free · stranded: 6.1 cores,
20 GB disk"). Worked example: an 8-core / 16 GB / 320 GB host at factor 1 with a
1 GB reserve holds **30 units** (RAM binds: 15360/512), stranding **6.125 cores** and
**20 GB disk**. Share units are never a placement input — feasibility and scoring
stay generic three-axis so Custom shapes keep working.

## Host-fact stamping

Real hosts start uncatalogued; the three totals are measured on the host and stamped
on the `Server`:

- **At bootstrap** — `bootstrap-server.py` carries `vcpus_total`,
  `memory_megabytes_total`, `pool_disk_gigabytes_total` on the `BootstrapResult`
  line (`atlas.hostfacts.host_capacity_facts`: `os.cpu_count`, `/proc/meminfo`
  MemTotal, `lvs -o lv_size` on the thin pool). `_absorb_bootstrap_output` stamps
  them.
- **On demand** — the **Refresh Capacity** button (`Server.refresh_capacity_facts`)
  runs the read-only `server-facts` Task and re-stamps all four numbers (the three
  totals + live `pool_data_percent`), without a re-bootstrap. `capacity_reported_at`
  records the measurement.

## The scorer (`placement.py::default_server` → `packing.rank_key`)

For each Active server, filter to those where the VM fits, then score:

```
reserve = server.placement_headroom_percent if > 0
          else Atlas Settings.placement_headroom_percent          # percent → /100
allowed(axis) = effective × (1 − reserve)                          # None = unmeasured = unlimited
fits          = every measured axis has used + need ≤ allowed
fill          = max over measured axes of (used + need) / allowed  # post-placement bottleneck
alignment     = Σ measured axes (need/effective)·(free/effective)  # demand-vs-headroom shape match
rank          = (unmeasured_axis_count, strategy_score, creation_index)   # min wins
```

Measured hosts always rank ahead of partially/unmeasured ones; `creation asc` breaks
final ties. `NoCapacityError` when nothing fits (Central reads "region full for that
size").

**Arrival reserve.** `placement_headroom_percent` (fleet default, per-server `> 0`
overrides) leaves headroom free on each host for a later in-place resize. Frappe
stores 0 for an untouched Percent field, so a per-server 0 means "inherit", not
"explicitly none" — an accepted trade-off. **Resize spends the reserve** (below).

### Strategies (`Atlas Settings.placement_strategy`)

`strategy_score` is pluggable; the operator picks, code doesn't change:

| Strategy      | Score          | Effect |
| ------------- | -------------- | ------ |
| **Spread** (default) | `+fill`   | Emptiest by relative fill → best blast radius + burst/resize headroom. Equal *relative* fill across heterogeneous hosts (for the proportional ladder, exact even spread in share units). |
| **Best Fit**  | `−fill`        | Fullest feasible host → keeps emptier hosts drainable and preserves large holes for Dedicated. |
| **Tetris**    | `−alignment`   | Dot-product alignment → matches demand shape to host headroom, least stranding on mixed hosts. |
| **First Fit** | `0` (creation) | The pre-scorer behaviour — cheapest, most fragmenting. |
| **Hybrid**    | per-VM         | Dedicated-class VMs (`cpu_max_cores ≥ 1`) Best Fit; the rest Spread. Resolved per-VM in `packing._resolve`. |

On the proportional ladder with **homogeneous** hosts the strategies achieve the
same total utilisation; they diverge only on **heterogeneous** fleets and on
drainability/defragmentation. Which to run is a data question — see the simulator.

**Hybrid, measured.** The idea was to best-fit the scarce large (Dedicated) holes
while spreading the churny shared tiers, hoping to cut forced resize-migrations. The
simulator (40 seeds, moderate load) says it does **not** cut migrations — it raises
them slightly vs Spread (≈ +4.5%, t ≈ 4), because concentrating Dedicated tightens
some hosts and steals in-place headroom from the shared VMs on them. Its real,
statistically-significant win is **Dedicated acceptance**: blocked-largest 0.49 vs
Spread's 0.52 (t ≈ 4.4) and slightly higher overall acceptance — at ~1/85th of Best
Fit's migration cost (≈ 974 vs 3556 migrations). So Hybrid is a low-migration middle
ground that protects Dedicated placement, **not** a migration reducer; Spread remains
the default and the one to pick when in-place resize headroom matters most.

## The resize gate (`virtual_machine.py::resize` → `check_resize_capacity`)

`resize()` checks capacity *before* running the on-host resize. It charges only the
**positive** per-axis deltas against the host's **full effective budget** (not the
placement `allowed` — the reserve exists precisely for the resize to spend):

```
delta(axis) = new − old        # cpu = cpu_max_cores or vcpus; memory; disk + data_disk
for each measured axis with delta > 0:  used + delta ≤ effective  else raise
```

`NoResizeCapacityError` (a `NoCapacityError` subclass — unchanged HTTP status/message
for the dashboard, but a distinct type) is the signal that the VM must **migrate to
grow**. A shrink needs no room; an unmeasured axis is unlimited.

## Validating strategy choice: the simulator

`atlas/atlas/packing_sim.py` is an event-driven simulator that places arrivals with
the **same** scorer (`packing.rank_key`), so a strategy that wins there is the one to
set on `placement_strategy`. It models the two events not yet wired into the app —
VM **drops** (per-plan exponential lifetimes) and **resizes** (an exponential upgrade
hazard → in-place if it fits, else a **forced migration**, else blocked) — because
they are cheap to model and are exactly what distinguishes the strategies. The
workload is generated once (seeded) and replayed under every strategy, so the
comparison is fair and reproducible.

It reports, per strategy: acceptance, **blocked-largest-plan rate** (the
fragmentation canary), in-place upgrade rate, **forced migrations**, mean committed
utilisation, mean hosts in use, and the **compaction ratio**
(`lower_bound_hosts / hosts_in_use`; ~1 tight, < 1 consolidatable). Metrics live in
`packing_metrics.py`. Run it (from the bench env) with configurable parameters:

```
python -m atlas.atlas.packing_sim --host-count 20 --arrival-rate 1.5 \
    --mean-lifetime 200 --upgrade-hazard 0.01 --reserve 10 \
    --host-shape 8,16384,320 --host-shape 16,32768,640   # repeat for a mixed fleet
```

Bootstrap the arrival/lifetime distributions from the Azure public traces
(github.com/Azure/AzurePublicDataset) until Atlas has its own telemetry; real
lifetimes are bimodal/heavy-tailed, not exponential — swap the sampler in
`generate_workload` when data exists.

## Explicitly out of scope — future migration work

Placement never moves a running VM. Two future cases (design only; build nothing
now):

- **Case 2 — resize needs migration.** `NoResizeCapacityError` is the trigger. Pick a
  target via the same scorer with the *new* size, run the spec/19 cold migration,
  then resize on the target — one orchestration carrying a `pending_resize` payload.
  The arrival reserve is what keeps this rare.
- **Case 3 — repack for better packing.** With a proportional catalog, repacking only
  improves *feasibility* by (a) draining a host or (b) defragmenting scattered free
  units so a 16-unit Dedicated fits. Future shape: an advisory rebalance report
  proposing the top-k migrations with benefit (max-relative-fill reduction / units
  defragmented) vs cost (∝ disk GB to hydrate). Operator-approved, never automatic.

## Deferred dials (not built)

- **Burst-density cap** — `sum(ceilings) ≤ max_ceiling_ratio × usable_cores`. Today
  `overprovision_factor` scales the guarantee axis; a true burst dial would bound the
  *ceilings* (the RELAXED `cpu.max`) separately. Note `overprovision_factor > 1`
  weakens "Dedicated" (it discounts a guaranteed core like shared bandwidth) — revisit
  when the factor is ever raised above 1.
- **Usage-based overcommit** — size the shared tier from predicted host peak
  (N-sigma / percentile of recent usage), not a fixed ratio (Bashir et al.,
  EuroSys'21). Guardrails to add when telemetry lands: host PSI, per-VM `cpu.stat`
  throttling, `memory.current` vs limit.
- **Lifetime-aware co-location** — predict short- vs long-lived at create time and
  segregate, so one long-lived VM doesn't pin an otherwise-churning host.
