# Placement strategy bake-off — findings

Offline evaluation backing PR #30's default `placement_strategy = Spread`. Every
scenario drives the **production scorer** (`atlas.atlas.packing.rank_key`, exactly as
`placement.default_server` calls it), so a strategy that wins here is the one to set on
`Atlas Settings.placement_strategy`. Fleet mirrors the 3 real DO droplets used to
validate (4 GB / 8 GB / 16 GB effective budgets). Every point is averaged over 16–24
seeds with sample stddev; the same seeded workload is replayed across all strategies.

Run (needs the bench venv so `frappe`/`atlas` import):

```
./env/bin/python llm/eval/sweep.py            # proportional ladder, heterogeneous fleet
./env/bin/python llm/eval/arbitrary_sim.py    # arbitrary (multi-dimensional) shapes
./env/bin/python llm/eval/arbitrary_lowload.py# arbitrary, low load (Tetris's niche)
./env/bin/python llm/eval/realistic_sim.py    # 80% fixed-small + 20% arbitrary + random migrations
./env/bin/python llm/eval/real_fleet_demo.py  # per-VM placement on the real 3-host fleet
```

## Conclusion: keep `Spread` as the default

The proportional size ladder makes packing one-dimensional, so on the ladder every
strategy achieves near-identical **utilisation** — the choice is decided by *secondary*
cost. Extending past the ladder to arbitrary and realistic mixed workloads, `Spread` is
at worst tied and usually strictly better.

### What each regime showed

- **Proportional ladder / arbitrary shapes** — acceptance is a statistical **tie** across
  all five strategies at every load. The scorer does not change *how many* VMs fit
  (capacity + per-axis demand do); it only changes operational cost. `Spread` minimises
  the costs that matter: **~3× fewer forced migrations** than Best Fit on the ladder
  (15–40% fewer under arbitrary shapes), best in-place-resize success, lowest per-host
  stranding/imbalance, lowest blast radius. Forced migration = a spec/24 cold migration
  (disk hydration over NBD/SSH) and fires on every resize that cannot grow in place — the
  dominant real cost.

- **Realistic composite** (80% small fixed-shape + 20% arbitrary, with drops, resizes,
  **and independent ad-hoc random migrations**) — `Spread` **wins acceptance outright**
  (~3 pp over Best Fit, beyond the error bars) and has the lowest **evacuation-failure
  rate** (`evac_fail_rate`: the share of ad-hoc single-VM migrations that find no target).
  Once the fleet is loaded enough that no host sits empty, Best Fit's headline virtue —
  "keeps hosts drainable" — loses at the single-VM level, because its tight packing leaves
  a VM nowhere to move; `Spread`'s per-host headroom absorbs the move.

- **The reserve is the real lever, not the strategy.** A 10% arrival-headroom reserve cut
  forced migrations ~8× and tripled in-place-resize success — a far bigger effect than any
  strategy swap (at a modest acceptance cost, and slightly higher evac difficulty).

### When to deviate (both edge cases; already operator-selectable, no code change)

- **Tetris** — only for a *lightly-loaded*, resize-light fleet dominated by lopsided
  *Custom* shapes. That is the sole regime where its dot-product shape-matching bought a
  small real acceptance edge (`arbitrary_lowload.py`, rate 0.06: 0.854 vs Spread 0.844).
  Everywhere else it is mid-pack.
- **Best Fit** — only if the operator deliberately wants to drain *whole hosts* (its empty
  hosts help there). For ad-hoc single-VM migrations it is the worst choice.
- **Hybrid ≈ Spread** in every regime — best-fitting only Dedicated VMs does not
  defragment when the spread *shared* VMs already scatter the holes. No reason to pick it
  over Spread.

### Bottom line

PR #30's `Spread` default is correct. The empirical justification now extends past the
proportional-ladder argument (which makes all strategies tie) into arbitrary and realistic
mixed workloads, where `Spread` is *genuinely better* rather than merely equivalent. The
actionable follow-up is not a strategy change — it is considering a non-zero default
`placement_headroom_percent` once resize-migration-on-demand exists.
