# The `/api/state` contract

The **single source of truth** for the shape of the JSON the dashboard renders.
Three producers must all emit this shape, or the UI silently degrades:

- **`backend/server.py`** — the shipped collector (real hosts, `build_state()`).
- **`backend/proxy.py`** — the dev SSH proxy (pushes `server.py --collect`, folds
  metrics). It re-shapes only `metrics`; everything else is passed through, so it
  inherits `server.py`'s shape exactly.
- **`mock/generate.mjs`** — the fixtures (`state-ordinary.json`, `state-scale.json`).

`mock/state-real.json` is a *capture* of a real host via `mock/collect-real.mjs`,
so it is a witness to the real shape, not a third producer.

The frontend (`src/derive.js` + the Vue components) is the **consumer**. Every
field it reads must be listed here and emitted by `server.py`, or it must degrade
to `—`/absent by design (see [§ Honest blanks](#honest-blanks)).

> **Rule of the contract:** the mock may not carry a field the real collector
> cannot produce. The mock's job is to exercise the UI against the *real* shape at
> *chosen cardinalities*, not to make the UI look better than a real host does.
> When they disagree, **`server.py` wins** — either teach the collector to emit
> the field, or delete the field from the mock and the UI. Never fake it.

---

## Top-level keys (all always present)

| Key | Type | Notes |
|---|---|---|
| `host` | object | host facts (versions, cpu/mem totals, uplink, packages) |
| `pool` | object \| null | flat thin-pool summary; `null` if no pool LV |
| `virtual_machines` | array | the subject of the page — see below |
| `images` | array | base image LVs |
| `snapshots` | array | warm-golden + disk + migrate snapshots |
| `volumes` | array | flat LV list |
| `storage` | object | layered `{ pvs[], vgs[], thin_pools[], volumes[] }` |
| `addresses` | array | `ip -j addr` |
| `interfaces` | array | `ip -j link` |
| `routes` | array | `ip -j route` (v4+v6) |
| `neigh_proxy` | array | proxy-NDP entries |
| `ip_rules` | array | `ip -j rule` |
| `reserved_ips` | array | reserved public v4 → guest |
| `proxy_maps` | array | **conditional** — see [§ Conditional sections](#conditional-sections) |
| `private_mesh` | array | **conditional** |
| `migrations` | array | **conditional** |
| `nft_tables` | array | one `inet/atlas` table, walked into human syntax |
| `units` | array | per-VM firecracker units + host oneshots/forwarders |
| `tasks` | array | **conditional** |
| `disks` | array | physical block devices |
| `users` | array | host users |
| `processes` | array | firecracker/jailer processes |
| `metrics` | object | `{ collected_at, series }` — see [§ Metrics](#metrics) |

---

## `virtual_machines[]` — the row the whole page joins on

Every VM row is a single dict literal, so **every key is always present**; what
varies is whether the *value* is real or `null`/`false`. Source column says where
`server.py` reads it and whether it can be `null`.

| Field | Type | Source (`server.py`) | Can be null? |
|---|---|---|---|
| `uuid` | string | jail dir name | no |
| `state` | string | `systemctl is-active firecracker-vm@<uuid>` | no (defaults `"Stopped"`) |
| `ipv6` | string | `network.env` `VIRTUAL_MACHINE_IPV6` | yes |
| `ipv4_guest` | string | env `IPV4_GUEST_CIDR` (stripped) | yes |
| `ipv4_host` | string | env `IPV4_HOST_CIDR` | yes |
| `reserved_ipv4` | string | env `RESERVED_IPV4` | yes |
| `mac` | string | derived from uuid | no |
| `tap_device` | string | env `TAP_DEVICE` | yes |
| `host_veth` | string | env `HOST_VETH` (`atlas-h<…>`) | yes |
| `namespace_veth` | string | env `NAMESPACE_VETH` | yes |
| `netns` | string | env `ATLAS_NETNS` | yes |
| `fc_uid` | int | env `ATLAS_FC_UID` | yes |
| `private_ipv6` | string | env `PRIVATE_IPV6` | yes |
| `image` | string | `_vm_image()` — **always `null`** (unlabeled hard link) | always null |
| `vcpus` | int | `firecracker.json` `vcpu_count` | yes |
| `mem_mib` | int | `firecracker.json` `mem_size_mib` | yes |
| `cgroup_cpu_max` | string | cgroup `cpu.max` (raw `"quota period"`) | yes |
| `cgroup_memory_max` | string | cgroup `memory.max` (raw bytes/`"max"`) | yes |
| `cpu_cap_cores` | float | parsed from `cgroup_cpu_max` (quota/period) | yes (null if `"max"`) |
| `mem_cap_mib` | int | parsed from `cgroup_memory_max` | yes (null if `"max"`) |
| `mem_used_mib` | int | cgroup `memory.current` | yes |
| `cpu_pct` | float | cgroup `cpu.stat` delta | **yes — needs two samples** |
| `disk_lv` | string | `atlas-vm-<uuid>` | no |
| `disk_origin` | string | batched `lvs` origin | yes |
| `disk_data_percent` | float | `lvs` `data_percent` | yes |
| `disk_size_bytes` | int | batched `lvs` `lv_size` | yes |
| `disk_used_bytes` | int | `lv_size` × `data_percent` | yes |
| `has_data_disk` | bool | `data.ext4` exists | no |
| `has_snapshot` | bool | `snapshot/` dir exists | no |
| `migrating` | bool | `migrating` marker file | no |
| `log_size` | int | size of `firecracker.log` | yes |

### The provisioning/tenancy fields are NOT in the contract

The following fields exist **only in the mock** and have **no `server.py` source**.
They are a fabricated model — the host cannot know tenancy, and there is no
on-host marker for the shared/dedicated provisioning class. Do not read them in
the frontend without a real collector path, and do not add them to the mock
without one:

`tenant`, `role`, `provisioning`, `cpu_request_cores`, `cpu_floor_cores`,
`cpu_weight_pct`, `cpu_max_pct`, `mem_request_mib`, `disk_allocated_bytes`.

> The frontend reads the real fields with the mock names as *fallback* — e.g.
> disk-committed prefers `disk_size_bytes` and falls back to
> `disk_allocated_bytes`; CPU/mem committed prefer `vcpus`/`mem_mib`. So the
> Provisioning panel is whole on real hosts (CPU `used` excepted — see below).

---

## Row shapes for the other sections

Only the fields the frontend reads are load-bearing; sources may carry more.

- **`host`** — `hostname`, `cpu_model`, `cpu_total` (int core count),
  `mem_total_mib`, `overprovision_factor`, `collected_at`, `uplink`, `packages{}`,
  version strings.
- **`pool`** — `vg`, `pool`, `backing` (`"loopback"`/`"device"`/`""`),
  `backing_device` (e.g. `/dev/md2`, or null on loopback), `size` (lvs string,
  e.g. `"886.24g"` — lvs's `<`/`~` approximation prefix is stripped),
  `data_percent`, `metadata_percent`.
- **`storage.thin_pools[]`** — `name`, `size_bytes`, `data_percent`,
  `metadata_percent`, `backing`.
- **`storage.pvs[]` / `vgs[]` / `volumes[]`** — `*_bytes` sizes (`size_bytes`,
  `used_bytes`, `free_bytes`), `name`, `role`, `lv_count`, `pv_count`.
- **`reserved_ips[]`** — `address`, `attached_vm`, `guest_ipv4` (the DNAT target),
  `anchor` (null on Scaleway), `anchor_gateway` (null).
- **`images[]`** — `name`, `kernel`, `rootfs`, `rootfs_size`, `base_lv`,
  `base_lv_size` (bytes).
- **`snapshots[]`** — `uuid`, `kind`, warm-golden fields; disk rows carry
  `snapshot_lv`, `origin_lv`, `data_percent`.
- **`units[]`** — `name`, `load`, `active`, `sub`, `kind`, `description` (the
  systemd description column; forwarder units carry their socat peer here, which
  `migrations()` reads back for `peer`).
- **`nft_tables[]`** — `{ family, name, persisted, chains[{ name, type, rules[] }] }`.
  Rules are human nft syntax (`ip daddr X dnat ip to Y`).

---

## <a id="conditional-sections"></a>Conditional sections (empty is correct)

These are `[]` unless a specific out-of-band dependency is live. The UI **must**
render an honest empty state, never assume they are populated:

| Section | Populated only when |
|---|---|
| `proxy_maps` | `ATLAS_PROXY_GUEST` env set **and** SSH to the proxy guest succeeds |
| `private_mesh` | VMs carry `PRIVATE_IPV6` (idea/private host-mesh) |
| `migrations` | a `atlas-migrate-forward@*` forwarder unit is live |
| `tasks` | `ATLAS_ROOT/tasks/recent.json` exists (host-side task tracking) |

---

## <a id="metrics"></a>Metrics

`server.py` (HTTP mode) and `proxy.py` both emit:

```
metrics: { collected_at, series: { <key>: { unit, points: [n, …] } } }
```

Series keys: `cpu_util_pct`, `mem_used_mib`, `disk_io_iops`, `net_rx_mbps`,
`net_tx_mbps`, `pool_used_pct`. A series appears **only once it has points**:
rate series (cpu/net/disk) need two polls, so they are absent on the first read.
Under raw `--collect` there is no `series` — only `metrics.raw` (the proxy folds
raw→series across its own poll history). The mock pre-fills a 48-point window;
that is the shape, not a claim of realtime.

---

## <a id="honest-blanks"></a>Honest blanks (leave as `—` by design)

Not recoverable from host disk — the UI shows `—`, never a fabricated value:

- **VM image name** (`image` always `null`). `disk_origin` (from `lvs`) is a
  partial answer for clones descending from `atlas-image-<name>`; warm clones
  descend from `atlas-snap-*` and lose the trail. "The server is a cache."
- **`cpu_pct` on a VM's first sample** — needs two cgroup readings.
- Any field whose backing file/env/binary is absent on a sparse host.

---

## <a id="open-decisions"></a>Field decisions — done and remaining

The "A" fixes (teach `server.py` to emit a recoverable field) are **done** — the
collector now emits every field below marked DONE. What remains is genuinely not
on the host.

| Field(s) | Status |
|---|---|
| `cpu_cap_cores`, `mem_cap_mib` | **DONE** — parsed server-side from the raw `cgroup_cpu_max`/`cgroup_memory_max`. |
| `disk_size_bytes`, `disk_used_bytes` | **DONE** — from the same batched `lvs` call that gives `disk_data_percent` (`used = size × data_percent`). |
| `cpu_model` (host) | **DONE** — from `/proc/cpuinfo`. |
| `pool.backing_device` | **DONE** — from `pool/pool-devices` (e.g. `/dev/md2`). |
| `reserved_ips[].guest_ipv4` | **DONE** — the DNAT target (the VM's `ipv4_guest`). |
| `images[].base_lv_size` | **DONE** — `lv_size` of the base LV. |
| `snapshots[].data_percent` | **DONE** — `lvs` `data_percent` on the snapshot LV. |
| `units[].description` | **DONE** (was a live bug) — the description column is now emitted, so `migrations().peer` resolves from the forwarder's socat line instead of always being null. |
| `cpu_pct` VM `used` | **Honest blank under the proxy.** It needs two cgroup samples; the dev SSH proxy pushes a single-shot `--collect`, so it reads `null`. The shipped HTTP `server.py` holds a per-cgroup previous sample across polls, so it *does* produce it. The Provisioning CPU "used" bar reads 0 only under the proxy — by design, not a gap. |
| `provisioning` (shared/dedicated), `cpu_request_cores`, `cpu_floor_cores`, `cpu_weight_pct`, `mem_request_mib`, `disk_allocated_bytes` | **DROPPED from the mock.** These encoded a request/cap policy the host doesn't record. The cap is real (`cpu_cap_cores`/`mem_cap_mib`); the shared/dedicated split is now **inferred from the cap** (`derive.js` `provisioningClass`: cap < vcpus ⇒ shared, cap == vcpus ⇒ dedicated), so the Provisioning panel is whole from host data alone. The frontend still honours an explicit `provisioning`/`*_request_*` field if a controller ever feeds one out-of-band — that path always wins over the inference. |
| `tenant`, `role` | **DROPPED from the mock AND the UI.** These were controller/DB facts with no host source. The tenant rail (label, operator facet, tenant search key) and the `vmTenant`/`isOperator`/`tenantSummary`/`tenantFacets` helpers are removed. Re-add only behind a documented controller feed. |

---

## Keeping producers in sync

There is no schema enforcement yet. The cheapest guard is a **shape-diff test**:
load a mock fixture and a `state-real.json` capture, assert the same top-level
keys and the same VM-row key set (modulo the documented conditional/honest-blank
fields). When this contract changes, update all three producers and re-capture
`state-real.json`.
