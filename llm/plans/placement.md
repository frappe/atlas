# Plan: load-aware VM placement for the shared-size ladder

Status: approved plan, ready to implement in one PR.
Read first: `spec/README.md`, `llm/Taste.md`, `atlas/atlas/placement.py`,
`atlas/atlas/api/server_capacity.py`, `atlas/atlas/sizes.py`.
Formatting: tab indent, double quotes, line-length 110; do **not** reformat
lines you didn't change (see `CLAUDE.md` — the ruff hook rewrites old files;
`git add -p` only your lines).

## 0. The one insight everything hangs on

Every size preset is an exact scalar multiple of one **share unit**
`(0.0625 cores, 512 MB, 10 GB)`:

| Preset       | units | cpu_max_cores | memory MB | disk GB |
| ------------ | ----- | ------------- | --------- | ------- |
| Shared 1x    | 1     | 0.0625        | 512       | 10      |
| Shared 2x    | 2     | 0.125         | 1024      | 20      |
| Shared 4x    | 4     | 0.25          | 2048      | 40      |
| Shared 8x    | 8     | 0.5           | 4096      | 80      |
| Dedicated 1x | 16    | 1             | 8192      | 160     |

Consequences (these are the design, not trivia):

1. **Packing is one-dimensional.** A host holds
   `U = min(cores×factor/0.0625, memory_mb/512, disk_gb/10)` share units, and
   *any* mix of preset VMs whose unit-sum ≤ U fits. There is no cross-axis
   fragmentation, so no bin-packing solver is needed or wanted. First-fit,
   best-fit, worst-fit — all achieve identical utilization for proportional
   items. Do not build anything cleverer than a scorer.
2. **Even-spread costs nothing.** Normally spreading fights consolidation;
   with proportional items the only packing cost of spreading is that a large
   VM (Dedicated 1x = 16 units) can be stranded by scattered free units even
   when fleet-total free ≥ 16. Accepted for now; the fix is case-3 migration
   (defrag), out of scope. `NoCapacityError` already tells Central "full for
   that size".
3. **Waste is a host-shape problem.** Whichever axis isn't the min of `U`
   strands resources. The ladder's balanced shape is **8 GB RAM per physical
   core (× overprovision factor) and 20 GB pool disk per GB RAM**. On a
   typical 2–4 GB/core cloud host, RAM binds and CPU-bandwidth is only 25–50%
   subscribed at full RAM. The fleet is mixed/undecided, so the deliverable is
   *visibility*: show per-host share-units and per-axis stranded amounts in
   Desk so the operator can compare shapes (step 5).
4. All ladder fractions (1/16 … 1/2) are exact binary floats; float sums are
   exact for presets. No integer-unit arithmetic needed in code. The unit is
   reporting sugar, not load-bearing: feasibility and scoring stay generic
   three-axis so Custom (operator-typed) shapes keep working.

## 1. What exists today (verified)

- `placement.py::default_server` — first Active server with room, ordered
  `creation asc`. This *stacks the oldest host until full*: worst possible for
  resize headroom and blast radius. Replace the pick, keep the contract
  (same signature, same `NoCapacityError`).
- `api/server_capacity.py::capacity_for_server` — per-axis
  `{total, effective, used}`; `effective is None` = uncatalogued = unlimited;
  CPU effective = total × `Atlas Settings.overprovision_factor`; used sums
  `cpu_max_cores` (bandwidth), `memory_megabytes`, `disk + data_disk` over
  non-Terminated VMs.
- `Server.vcpus_total / memory_megabytes_total / pool_disk_gigabytes_total`
  fields exist (server.json) but **nothing stamps them on real hosts** — only
  `providers/fake.py::fake_host_totals` synthesizes them and tests set them.
  Real hosts are unlimited on every axis (CPU falls back to the hand-kept
  `DIGITALOCEAN_VCPUS_BY_SIZE` slug dict). `pool_data_percent` is likewise
  never written.
- `virtual_machine.py::resize()` (~line 683) — **no capacity check at all**.
  A resize can silently oversubscribe RAM/disk on a full host.
- Bootstrap result plumbing: `scripts/bootstrap-server.py` emits a typed
  `BootstrapResult` (dataclass, ~line 200) as one `ATLAS_RESULT=` JSON line;
  `server.py::_absorb_bootstrap_output` (~line 321) parses it and stamps
  Server fields. Extend this exact path.

## 2. Step 1 — stamp host capacity facts at bootstrap

Files: `scripts/bootstrap-server.py`, `atlas/atlas/doctype/server/server.py`,
`atlas/atlas/doctype/server/server.js`, a new `scripts/server-facts.py`.

- Add to `BootstrapResult`: `vcpus_total: int`, `memory_megabytes_total: int`,
  `pool_disk_gigabytes_total: int`. Sources on the host: `os.cpu_count()` (or
  nproc), `MemTotal` from `/proc/meminfo` (kB → MB), and the thin pool's size
  via `lvs --units b --nosuffix -o lv_size` on the pool the bootstrap script
  itself creates — read bootstrap-server.py to get the exact VG/LV names; do
  not guess them. Bootstrap re-run refreshes the facts (idempotent).
- `_absorb_bootstrap_output` stamps the three fields (raw physical facts —
  policy like reserves does NOT live on the row).
- New task script `scripts/server-facts.py` reporting the same three numbers
  (+ `pool_data_percent` from `lvs -o data_percent`, closing that dead field),
  a whitelisted `Server.refresh_capacity_facts()` that runs it and stamps, and
  a **Refresh Capacity** button in server.js for already-Active hosts. New
  host task scripts auto-ship via `scripts_catalog` (see
  `_absorb_bootstrap_output`'s upload comments) — after editing, live hosts
  need `sync_scripts` / re-bootstrap.
- Per spec/README "Desk-button coverage": the new button's happy path goes in
  the `desk_buttons` e2e use case, and `server_provisioning.run` asserts the
  three totals are non-empty after bootstrap (a host fact).

## 3. Step 2 — new fields: reserve + memory floor

Files: `atlas_settings.json`, `server.json` (+ their .py where validation
lives).

- `Atlas Settings.placement_headroom_percent` — Percent, default **0** (no
  reserve). Fraction of each measured axis that *new-VM placement* leaves
  free for future in-place resizes; resize itself ignores it (step 4).
- `Server.placement_headroom_percent` — Percent, no default. **> 0 wins over
  the fleet setting; 0/unset inherits.** (Frappe writes 0 on save for
  untouched Float/Percent fields, so "explicitly 0, fleet default > 0" is not
  expressible per-host — document this in the field description; it was an
  accepted trade-off.)
- `Atlas Settings.host_memory_reserve_megabytes` — Int, default **1024**.
  Subtracted from the memory axis's effective budget in
  `capacity_for_server` (`effective = total − reserve`, clamped ≥ 0; total
  stays raw). Guest RAM must never pack to 100% of MemTotal — the host OS,
  per-VM Firecracker/jailer overhead, and thin-pool metadata live there too;
  packing to MemTotal OOMs the host. 1024 is a guess; measuring real per-VM
  overhead is listed under Open questions.

## 4. Step 3 — replace first-fit with relative-fill spread

File: `atlas/atlas/placement.py` (keep `default_server`'s signature and
`NoCapacityError` contract; `apply_user_defaults` unchanged).

For each Active server, from `capacity_for_server(server)`:

```
reserve   = server.placement_headroom_percent if > 0
            else Atlas Settings.placement_headroom_percent   # percent → /100
budget(axis) = axis.effective × (1 − reserve)   # None stays None (unmeasured)
fits: for every axis, budget is None or used + need ≤ budget
score = max over measured axes of (used + need) / budget     # post-placement fill
rank  = (count of unmeasured axes, score, creation)          # min wins
```

- Feasibility stays three-axis (`_fits` shape) so Custom VMs keep working.
- The winner is the feasible server with the lowest post-placement fill on
  its bottleneck axis → equal *relative* fill across heterogeneous hosts;
  for preset VMs this is exactly even spread in share units.
- Fully-measured hosts rank ahead of partially/unmeasured ones (same
  precedent as `largest_vm`'s measured-first ranking and its comment).
  `creation asc` last for determinism.
- Raise `NoCapacityError` when nothing fits, as today. `budget == 0` on any
  measured axis with `need > 0` simply fails `fits`.

## 5. Step 4 — the resize capacity gate (the real bug fix)

Files: `atlas/atlas/placement.py`,
`atlas/atlas/doctype/virtual_machine/virtual_machine.py::resize()`.

- New `class NoResizeCapacityError(NoCapacityError)` in placement.py — Central
  catches `NoCapacityError` today; the subclass adds the "this VM needs a
  migration to grow" signal without changing HTTP status or message shape.
- New helper in placement.py (capacity policy lives in one module), called
  from `resize()` *before* `run_task`:

```
capacity = capacity_for_server(vm.server)      # used includes this VM already
delta_cpu    = new_cpu_max − (vm.cpu_max_cores or vm.vcpus)
delta_memory = new_memory − vm.memory_megabytes
delta_disk   = (new_disk + new_data_disk) − (vm.disk_gigabytes + vm.data_disk_gigabytes)
for each measured axis with delta > 0:
    used + delta ≤ effective          # FULL budget — the reserve is spent here
else raise NoResizeCapacityError
```

- Resize deliberately checks `effective`, not the placement budget: the
  headroom reserve exists precisely so resizes can consume it.
- Negative deltas need no room (and today disk can't shrink anyway).

## 6. Step 5 — Desk visibility of share units and stranded resources

Files: `atlas/atlas/sizes.py`, `atlas/atlas/api/server_capacity.py`, the
capacity display consumer (find where `capacity_for_server` /
`cluster_capacity` render — start from `virtual_machine.js`).

- In sizes.py, derive the unit from the ladder itself (one source of truth):
  `SHARE_UNIT = SIZE_PRESETS["Shared 1x"]` costs on the three axes.
- `capacity_for_server` gains, when at least one axis is measured:
  - `share_units`: `{total, used, free}` — total = floor(min over measured
    axes of `effective / unit_cost`), used = same min-rule over `used`,
    computed per axis... **keep it simple**: units_total = floor(min over
    measured axes of effective/unit); units_used = ceil(max over measured
    axes of used/unit); free = total − used.
  - `stranded`: per measured axis, `effective − units_total × unit_cost` —
    the resources the bottleneck axis makes unsellable at full subscription.
- `cluster_capacity` sums them; the Desk capacity block shows one extra line
  (e.g. "34 / 60 units free · stranded: 6.1 cores, 20 GB disk"). The fleet is
  shape-undecided; this line is how the operator compares host shapes.
- Do not make `share_units` a placement input. Reporting only.

## 7. Step 6 — spec updates (spec is the source of truth)

- New `spec/28-placement.md`: the share-unit model and the proportionality
  invariant; the relative-fill scorer and ranking; reserve semantics
  (fleet default, per-server >0-wins override, resize spends the reserve);
  the memory floor; host-fact stamping (bootstrap + Refresh Capacity); the
  resize gate + `NoResizeCapacityError`; the balanced-host-shape math
  (8 GB/core × factor, 20 GB disk per GB RAM) with a worked stranded example;
  future cases 2/3 (below) as deliberate deferrals.
- `spec/README.md`: add 24 to the reading list; **revise the non-goals
  bullet** "No autoscaling or scheduling … first Active server with room (a
  default, not a scheduler)" — placement is now load-aware spread, still not
  a scheduler (no queues, no reactive rebalancing); link 24. Add the
  Refresh Capacity row to the operator use-case table.
- `spec/11-user-ui.md` (~line 45): the placement-model pointer ("fills
  `server`… from a vCPU-budget") → point at 24.
- `spec/05-virtual-machine-lifecycle.md`: document the resize capacity gate
  where resize is specced.

## 8. Step 7 — tests

Unit (`bench --site atlas.tests.local run-tests --app atlas`, must pass):

- `atlas/tests/test_sizes.py`: **pin the proportionality invariant** — every
  preset's `(cpu_max_cores/0.0625, memory/512, disk/10)` is the same integer.
  Failure message must say the placement scorer's even-spread-is-free
  property assumes this; breaking it deliberately means revisiting spec/28.
- `atlas/tests/test_placement.py` (Fake provider hosts, `fake_host_totals`):
  alternating spread across two equal hosts; relative fill on heterogeneous
  hosts (big host absorbs proportionally more); fleet reserve blocks a new
  VM that raw effective would admit; per-server override >0 beats fleet
  default; unmeasured host ranks behind measured; deterministic tie-break;
  `NoCapacityError` when full.
- `atlas/tests/test_api_server_capacity.py`: memory effective = total −
  reserve (clamp at 0); `share_units` + `stranded` math incl. the worked
  example (8c/16GB/320GB, factor 1, reserve 1024 → 30 units, ~6.1 cores +
  20 GB disk stranded); partial measurement → units over measured axes only.
- `doctype/virtual_machine/test_virtual_machine.py`: resize within capacity
  passes; over-capacity raises `NoResizeCapacityError`; mixed deltas (grow
  RAM, CPU unchanged) only charge positive deltas; resize succeeds inside the
  reserve that placement refused.

E2E (do not run in CI here; note them for the operator):
`server_provisioning.run` asserts stamped totals post-bootstrap;
`desk_buttons` gains the Refresh Capacity button path.

## 9. Explicitly out of scope — future migration work (design notes only)

- **Case 2 — resize needs migration.** `NoResizeCapacityError` is the
  trigger. Future flow: pick a target via the same scorer with the *new*
  size as the requirement, run the spec/19 cold migration (dm-clone over
  NBD/SSH — machinery already on this branch), then resize on the target;
  one orchestration on the Virtual Machine Migration row carrying a
  `pending_resize` payload. The headroom reserve is what keeps this rare.
- **Case 3 — migrate for globally better packing.** With a proportional
  catalog, repacking never improves *feasibility* except (a) draining a host
  and (b) defragmenting scattered free units so a 16-unit VM fits. Future
  shape: an advisory rebalance report (whitelisted method) proposing the
  top-k migrations with benefit (reduction in max relative fill / units
  defragmented) vs cost (∝ disk GB to hydrate). Operator-approved, never
  automatic. Build nothing now.

## 10. Pushback recorded during planning

1. `resize()` having no capacity check is the most important fix in this
   plan, not the scorer.
2. `overprovision_factor` is a **no-op for shared tiers** on hosts with
   ≤ 8 GB RAM per core — RAM binds first. Don't tune it expecting density.
3. Related smell (not fixed here): `factor > 1` weakens "Dedicated" — the
   CPU axis discounts a Dedicated core the same as shared bandwidth, so a
   guaranteed core isn't guaranteed under oversubscription. Revisit when the
   factor is ever raised above 1 (e.g. apply it to the shared portion only).
4. No bin-packing solver, no ILP, no vector-packing heuristics (dot-product /
   cosine): pointless for a proportional catalog. Revisit only if the ladder
   breaks proportionality — the generic three-axis scorer already degrades
   gracefully for Custom shapes.
5. "Minimise free memory" must mean minimise *unsellable* memory; running the
   host at zero free RAM is an OOM, hence the memory floor.

## 11. Open questions (not blockers)

- Measure real per-VM host overhead (firecracker + jailer RSS, page tables,
  thin-pool metadata) on a live host to replace the 1024 MB guess.
- Does the fleet want a non-zero default reserve once migration (case 2)
  exists, or is migrate-on-demand cheap enough to keep packing at 100%?
- Host shape purchasing: the stranded line in Desk (step 5) is the data;
  revisit the ladder or the shapes once a quarter of real numbers exists.
