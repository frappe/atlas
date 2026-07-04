// ═══════════════════════════════════════════════════════════════════════════
// generate.mjs — build the mock host fixtures in the CURRENT (v2) shape.
//
// One generator, one shape, parameterised only by CARDINALITY. It emits the two
// synthetic sources the dashboard ships with; the third source (Real Host) is a
// live SSH collection, not generated here (see mock/collect-real.mjs).
//
//   node mock/generate.mjs ordinary   # ~24 VMs  → mock/state-ordinary.json
//   node mock/generate.mjs scale      # ~1000 VMs → mock/state-scale.json
//   node mock/generate.mjs            # both
//
// The shape is a strict SUPERSET of the v1 contract (the existing UI renders it
// unchanged) and matches exactly what backend/server.py's build_state() emits:
//
//   1. Firewall reads like `nft list`     — nft rules are readable text, always.
//   2. Overprovision is honest            — every VM carries its committed cgroup
//      caps AND its live actual usage (mem_used_mib / cpu_pct). The host is
//      deliberately OVER-committed (Σ caps ≫ physical) yet lightly USED — the
//      "most resources are overprovisioned" story, shown as cap-vs-actual.
//   3. Pool reads its true fill           — thin pool sized to the fleet, never 100%.
//   4. Storage is layered                 — storage.{pvs,vgs,thin_pools,volumes},
//      real byte sizes at every layer (PV → VG → pool → LV), not one flat line.
//   5. Proxy maps are live routes         — populated proxy_maps, both http `sites`
//      and stream SNI, each a real published hostname → backend.
//
// Deterministic (a fixed seed) so re-running yields byte-identical output — each
// fixture is a checked-in contract example, not a fresh random dump each time.
// The seed is re-primed per profile so the two files are independent yet stable.
// ═══════════════════════════════════════════════════════════════════════════

import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

// ── profiles — the ONLY thing that varies between fixtures is cardinality ─────
const PROFILES = {
  ordinary: {
    vms: 24,
    out: "state-ordinary.json",
    hostname: "f3-aditya-blr3",
    seed: 0x5f2a91c3,
  },
  scale: {
    vms: 1000,
    out: "state-scale.json",
    hostname: "f9-aditya-blr3",
    seed: 0x9e3779b1,
  },
};

// ── deterministic RNG (mulberry32) — no Date/Math.random, reproducible ───────
let _seed = 0;
function rnd() {
  _seed |= 0;
  _seed = (_seed + 0x6d2b79f5) | 0;
  let t = Math.imul(_seed ^ (_seed >>> 15), 1 | _seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
const between = (lo, hi) => lo + Math.floor(rnd() * (hi - lo + 1));

// ── host physical facts — a real Scaleway EM box, deliberately over-committed ─
// 6 physical cores, 32 GiB RAM. overprovision_factor 4 → the budget the operator
// sells against is 24 vCPU / 128 GiB. We then commit MORE than physical (the
// point of over-provisioning) while actual usage stays low.
const CPU_TOTAL = 6;
const MEM_TOTAL_MIB = 32 * 1024;
const OVERPROVISION = 4;

const GiB = 1024 * 1024 * 1024;
const MiB = 1024 * 1024;

// ── the VM fleet ─────────────────────────────────────────────────────────────
const TENANTS = [
  "acme-01",
  "acme-01",
  "globex",
  "initech",
  "umbrella",
  "hooli",
  "hooli",
  "stark",
];
const SIZES = [
  // [vcpus, mem_mib] — the committed cap. Real fleets are a spread of sizes.
  [1, 1024],
  [1, 2048],
  [2, 2048],
  [2, 4096],
  [4, 8192],
];
const IMAGE_ORIGINS = [
  "atlas-snap-mjmdd1p7fb",
  "atlas-snap-e36kce7fhk",
  "atlas-snap-k29fbx7t1a",
  "atlas-image-ubuntu-24.04",
];

const HOST_V6_PREFIX = "2001:bc8:1203:15";
const UPLINK = "enp3s0f0np0";

// A stable uuid from a counter (deterministic, valid v4 shape).
function uuidFrom(n) {
  const h = (n * 0x9e3779b1) >>> 0;
  const s = h.toString(16).padStart(8, "0");
  const s2 = ((h ^ 0x5bd1e995) >>> 0).toString(16).padStart(8, "0");
  return `${s}-${s2.slice(0, 4)}-4${s2.slice(4, 7)}-8${s.slice(
    0,
    3
  )}-${s2}${s.slice(0, 4)}`;
}
const uuid8 = (u) => u.slice(0, 8);

// A private v4 /10 (100.64.0.0/10) address from a flat VM index — CGNAT space,
// so it scales cleanly to 1000+ VMs without overflowing a single octet.
function cgnat(index) {
  // index → 100.64.<b>.<c> ; two guests per VM (host .odd, guest .even).
  const n = index & 0x3fff; // 14 bits fit inside the /10
  return `100.64.${(n >> 8) & 0xff}.${n & 0xff}`;
}

function buildVms(N) {
  const vms = [];
  for (let i = 0; i < N; i++) {
    const u = uuidFrom(i + 1);
    const h7 = uuid8(u).slice(0, 7);
    // Two operator VMs (proxy + vpn gateway); the rest are customer fleet.
    const isProxy = i === 0;
    const isVpn = i === 1;
    const operator = isProxy || isVpn;
    const [vcpus, mem_mib] = operator ? [2, 2048] : pick(SIZES);

    // State: mostly running, a small fraction stopped, a rare failure — a
    // realistic mix that holds its proportions at any fleet size.
    let state = "Running";
    const roll = rnd();
    if (!operator) {
      if (roll < 0.02) state = "Failed";
      else if (roll < 0.1) state = "Stopped";
    }

    // Live actual usage — LOW relative to the cap (the overprovision story).
    // A running VM uses 5–35% of its RAM cap and 1–20% CPU; stopped/failed use 0.
    const running = state === "Running";
    const mem_used_mib = running
      ? Math.round(mem_mib * (0.05 + rnd() * 0.3))
      : 0;
    const cpu_pct = running ? Math.round((1 + rnd() * 19) * 10) / 10 : 0;

    // cgroup caps in the raw units the host reads them.
    const cgroup_memory_max = String(Math.round(mem_mib * 1.15) * MiB); // fc overhead
    const cgroup_cpu_max = `${vcpus * 100000} 100000`; // quota period (µs), Hard cap

    // ── overprovision model (#2) — the request→cap story, per VM ─────────────
    // A `shared` VM reserves a 25% CPU FLOOR and bursts to its cap (vcpus); a
    // `dedicated` VM pins exact (request == cap). Memory is always a hard cap
    // (request == cap == mem_mib). Most of the fleet is shared — that's the
    // whole point of overcommit. Operator VMs are dedicated (infra, no burst).
    const dedicated = operator || rnd() < 0.18;
    const provisioning = dedicated ? "dedicated" : "shared";
    const cpu_cap_cores = vcpus;
    const cpu_request_cores = dedicated
      ? vcpus
      : Math.round(vcpus * 0.25 * 100) / 100;
    const cpu_floor_cores = cpu_request_cores;
    const cpu_weight_pct = Math.round((cpu_request_cores / vcpus) * 100);
    const cpu_max_pct = 100;
    const mem_cap_mib = mem_mib;
    const mem_request_mib = mem_mib; // hard cap, request == cap
    // Thin disk: the whole cap is allocated; live fill is data_percent of it.
    const disk_allocated_bytes = 28 * GiB;
    const disk_used_bytes = running
      ? Math.round(disk_allocated_bytes * ((8 + rnd() * 80) / 100))
      : 0;

    const hex = uuid8(u).slice(0, 2);
    vms.push({
      uuid: u,
      state,
      tenant: operator ? "operator" : pick(TENANTS),
      role: isProxy ? "reverse-proxy" : isVpn ? "vpn-gateway" : null,
      ipv6: `${HOST_V6_PREFIX}::${(0x20 + i).toString(16)}`,
      ipv4_guest: cgnat(2 + i * 2),
      ipv4_host: cgnat(1 + i * 2),
      reserved_ipv4: isProxy ? "51.159.76.127" : null,
      mac: `06:00:${hex}:${h7.slice(2, 4)}:${h7.slice(4, 6)}:00`,
      tap_device: `atlas-${uuid8(u)}`,
      host_veth: `atlas-h${h7}`,
      namespace_veth: `atlas-n${h7}`,
      netns: `atlas-${uuid8(u)}${h7}`,
      fc_uid: 257000 + i,
      private_ipv6: isVpn ? null : `fdaa::${(0x100 + i).toString(16)}`,
      image: null,
      // Committed size + caps.
      vcpus,
      mem_mib,
      cgroup_cpu_max,
      cgroup_memory_max,
      // The overprovision model — request/cap/floor per resource (#2).
      provisioning,
      cpu_request_cores,
      cpu_cap_cores,
      cpu_floor_cores,
      cpu_weight_pct,
      cpu_max_pct,
      mem_request_mib,
      mem_cap_mib,
      // Live actual usage — the honest denominator (#2).
      mem_used_mib,
      cpu_pct,
      // Disk.
      disk_lv: `atlas-vm-${u}`,
      disk_origin: operator ? "atlas-image-ubuntu-24.04" : pick(IMAGE_ORIGINS),
      disk_data_percent: running
        ? Math.round((disk_used_bytes / disk_allocated_bytes) * 10000) / 100
        : null,
      disk_size_bytes: 28 * GiB,
      disk_allocated_bytes,
      disk_used_bytes,
      has_data_disk: !operator && rnd() < 0.3,
      has_snapshot: rnd() < 0.5,
      // One in-flight migration on the fleet (the first non-operator running VM).
      migrating: i === 5,
      log_size: `${between(2, 40)}.${between(0, 9)}k`,
    });
  }
  return vms;
}

// ── firewall — readable nft rules (#1), the exact text `nft list` prints ──────
function buildNft(VMS) {
  const forward = ["ip daddr 169.254.169.254 drop"];
  const prerouting = [];
  const postrouting = [];
  for (const vm of VMS) {
    if (vm.state !== "Running") continue;
    forward.push(`ip6 daddr ${vm.ipv6} oifname "${vm.host_veth}" accept`);
    forward.push(`ip6 saddr ${vm.ipv6} iifname "${vm.host_veth}" accept`);
    if (vm.reserved_ipv4) {
      prerouting.push(
        `ip daddr ${vm.reserved_ipv4} dnat ip to ${vm.ipv4_guest}`
      );
      postrouting.push(
        `ip saddr ${vm.ipv4_guest} snat ip to ${vm.reserved_ipv4}`
      );
    }
  }
  postrouting.push(`ip saddr 100.64.0.0/10 oifname "${UPLINK}" masquerade`);
  return [
    {
      family: "inet",
      name: "atlas",
      persisted: false,
      chains: [
        { name: "forward", type: "filter", rules: forward },
        { name: "prerouting", type: "nat", rules: prerouting },
        { name: "postrouting", type: "nat", rules: postrouting },
      ],
    },
  ];
}

// ── storage — the full LVM hierarchy (#3, #4) ─────────────────────────────────
function buildStorage(VMS, snapDescs) {
  // Two NVMe drives in a RAID1 mirror → one PV → the `atlas` VG → thin pool0.
  const PV_SIZE = 953 * GiB; // ~1TB NVMe usable
  const vgSize = PV_SIZE;
  const volumes = [];
  // Image base LVs.
  for (const name of [
    "ubuntu-24.04",
    "ubuntu-24.04-minimal",
    "bench-nightly-admin-develop",
  ]) {
    volumes.push({
      name: `atlas-image-${name}`,
      vg: "atlas",
      size: humanBytes(28 * GiB),
      size_bytes: 28 * GiB,
      role: "image",
      origin: null,
      data_percent: null,
    });
  }
  // Per-VM disks (thin, so provisioned size is the cap; fill is data_percent).
  let poolUsedBytes = 0;
  for (const vm of VMS) {
    const size = vm.disk_size_bytes;
    const fill = vm.disk_data_percent || 0;
    poolUsedBytes += Math.round(size * (fill / 100));
    volumes.push({
      name: vm.disk_lv,
      vg: "atlas",
      size: humanBytes(size),
      size_bytes: size,
      role: "vm-disk",
      origin: vm.disk_origin,
      data_percent: vm.disk_data_percent,
    });
  }
  // Disk snapshots (from the shared descriptors so snapshots + storage agree).
  for (const s of snapDescs) {
    volumes.push({
      name: s.snapshot_lv,
      vg: "atlas",
      size: humanBytes(28 * GiB),
      size_bytes: 28 * GiB,
      role: "snapshot",
      origin: s.origin_lv,
      data_percent: s.data_percent,
    });
    poolUsedBytes += Math.round(28 * GiB * (s.data_percent / 100));
  }

  // The thin pool sizes ~72% of the VG; its data_percent is the TRUE fill.
  // At scale the fill can exceed the pool — clamp reporting to a plausible max
  // (a real over-full pool would have refused new allocations well before 100%).
  const poolSize = Math.round(vgSize * 0.72);
  const dataPercent = Math.min(
    98.5,
    Math.round((poolUsedBytes / poolSize) * 10000) / 100
  );
  volumes.push({
    name: "pool0",
    vg: "atlas",
    size: humanBytes(poolSize),
    size_bytes: poolSize,
    role: "pool",
    origin: null,
    data_percent: dataPercent,
  });

  const vgUsed = poolSize; // the pool is what's carved from the VG
  return {
    storage: {
      pvs: [
        {
          name: "/dev/md2",
          vg: "atlas",
          size_bytes: PV_SIZE,
          free_bytes: vgSize - vgUsed,
          used_bytes: vgUsed,
        },
      ],
      vgs: [
        {
          name: "atlas",
          size_bytes: vgSize,
          free_bytes: vgSize - vgUsed,
          used_bytes: vgUsed,
          pv_count: 1,
          lv_count: volumes.length,
        },
      ],
      thin_pools: [
        {
          name: "pool0",
          vg: "atlas",
          size_bytes: poolSize,
          backing: "device",
          data_percent: dataPercent,
          metadata_percent: Math.round((2 + rnd() * 4) * 100) / 100,
        },
      ],
      volumes,
    },
    // The flat `pool` + `volumes` the current UI still reads (v1 compat).
    pool: {
      vg: "atlas",
      pool: "pool0",
      backing: "device",
      backing_device: "/dev/md2",
      size: humanBytes(poolSize),
      data_percent: dataPercent,
      metadata_percent: 3.1,
    },
    volumes,
  };
}

function humanBytes(n) {
  let v = n;
  for (const u of ["", "k", "m", "g", "t"]) {
    if (v < 1024 || u === "t") return u ? `${v.toFixed(2)}${u}` : String(v);
    v /= 1024;
  }
}

// Snapshot descriptors — a fixed pair, pointing at whatever VMs exist. Kept in
// one place so snapshots[], storage volumes, and images all agree.
function buildSnapshots(VMS) {
  const a = VMS[Math.min(3, VMS.length - 1)];
  const b = VMS[Math.min(9, VMS.length - 1)];
  return [
    {
      uuid: "e36kce7fhk",
      kind: "disk",
      snapshot_lv: "atlas-snap-e36kce7fhk",
      origin_lv: a.disk_lv,
      data_percent: 21.54,
    },
    {
      uuid: "mjmdd1p7fb",
      kind: "disk",
      snapshot_lv: "atlas-snap-mjmdd1p7fb",
      origin_lv: b.disk_lv,
      data_percent: 33.9,
    },
  ];
}

// ── live proxy maps (#5) — real published routes off the proxy guest ──────────
function buildProxyMaps(VMS) {
  const rows = [];
  const subdomains = [
    "acme-01",
    "globex",
    "initech",
    "umbrella",
    "hooli",
    "stark",
  ];
  // http `sites`: subdomain → a customer VM's guest /128 on :443.
  subdomains.forEach((sub, i) => {
    const vm =
      VMS.filter((v) => v.tenant === sub && v.state === "Running")[0] ||
      VMS[Math.min(i + 2, VMS.length - 1)];
    rows.push({
      listen: ":443",
      protocol: "https",
      sni: `${sub}.blr3.frappe.dev`,
      backend: `[${vm.ipv6}]:443`,
      vm: vm.uuid,
    });
  });
  // stream SNI: a couple of custom domains passed through by SNI.
  [
    [
      "app.acme.com",
      VMS.find((v) => v.tenant === "acme-01" && v.state === "Running"),
    ],
    [
      "shop.globex.io",
      VMS.find((v) => v.tenant === "globex" && v.state === "Running"),
    ],
  ].forEach(([domain, vm]) => {
    if (!vm) return;
    rows.push({
      listen: ":443",
      protocol: "sni-passthrough",
      sni: domain,
      backend: `[${vm.ipv6}]:443`,
      vm: vm.uuid,
    });
  });
  return rows;
}

// ── metrics — an accumulated window that agrees with the fleet's actual usage ─
function buildMetrics(VMS, poolPct) {
  const N = 48;
  const series = {};
  const totalMemUsed = VMS.reduce((s, v) => s + (v.mem_used_mib || 0), 0);
  const totalCpu = VMS.reduce((s, v) => s + (v.cpu_pct || 0), 0);
  // Host cpu% ≈ committed load spread over physical cores; stays modest.
  const cpuBase = Math.min(85, totalCpu / CPU_TOTAL);
  const wobble = (base, amp) => {
    const pts = [];
    for (let i = 0; i < N; i++)
      pts.push(
        Math.round((base + Math.sin(i / 4) * amp + (rnd() - 0.5) * amp) * 100) /
          100
      );
    return pts.map((p) => Math.max(0, p));
  };
  series.cpu_util_pct = { unit: "%", points: wobble(cpuBase, 8) };
  series.mem_used_mib = {
    unit: "MiB",
    points: wobble(totalMemUsed, totalMemUsed * 0.06),
  };
  series.disk_io_iops = { unit: "iops", points: wobble(420, 260) };
  series.net_rx_mbps = { unit: "Mb/s", points: wobble(180, 120) };
  series.net_tx_mbps = { unit: "Mb/s", points: wobble(95, 70) };
  series.pool_used_pct = { unit: "%", points: wobble(poolPct, 0.3) };
  return { collected_at: "2026-07-04T12:00:00Z", series };
}

// ── assemble one fixture for a profile ────────────────────────────────────────
function buildState(profile) {
  _seed = profile.seed;
  const VMS = buildVms(profile.vms);
  const PROXY = VMS[0];
  const snapshots = buildSnapshots(VMS);
  const store = buildStorage(VMS, snapshots);

  return {
    host: {
      hostname: profile.hostname,
      collected_at: "2026-07-04T12:00:00Z",
      firecracker_version: "v1.16.0",
      jailer_version: "v1.16.0",
      kernel_version: "6.8.0-88-generic",
      linux: "Ubuntu 24.04.3 LTS",
      architecture: "x86_64",
      python_version: "Python 3.14.3",
      uplink: UPLINK,
      cpu_model: "AMD EPYC 4245P 6-Core Processor",
      cpu_total: CPU_TOTAL,
      mem_total_mib: MEM_TOTAL_MIB,
      overprovision_factor: OVERPROVISION,
      packages: [
        { name: "firecracker", version: "v1.16.0" },
        { name: "nftables", version: "1.0.9" },
        { name: "lvm2", version: "2.03.16(2)" },
        { name: "iproute2", version: "6.1.0" },
      ],
    },
    pool: store.pool,
    virtual_machines: VMS,
    images: [
      {
        name: "ubuntu-24.04",
        kernel: "vmlinux-noble-server",
        rootfs: "ubuntu-24.04-server.ext4",
        rootfs_size: "4.00g",
        base_lv: "atlas-image-ubuntu-24.04",
        base_lv_size: "4.00g",
      },
      {
        name: "ubuntu-24.04-minimal",
        kernel: "vmlinux-noble-minimal",
        rootfs: "ubuntu-24.04-minimal.ext4",
        rootfs_size: "4.00g",
        base_lv: "atlas-image-ubuntu-24.04-minimal",
        base_lv_size: "4.00g",
      },
      {
        name: "bench-nightly-admin-develop",
        kernel: null,
        rootfs: null,
        rootfs_size: null,
        base_lv: "atlas-image-bench-nightly-admin-develop",
        base_lv_size: "28.00g",
      },
    ],
    snapshots,
    volumes: store.volumes,
    storage: store.storage,
    addresses: [
      {
        interface: UPLINK,
        family: "inet",
        address: "51.159.202.203/24",
        scope: "global",
      },
      {
        interface: UPLINK,
        family: "inet6",
        address: `${HOST_V6_PREFIX.replace(
          ":15",
          ":743"
        )}:9e6b:ff:febd:fe41/64`,
        scope: "global",
      },
      {
        interface: UPLINK,
        family: "inet6",
        address: "fe80::9e6b:ff:febd:fe41/64",
        scope: "link",
      },
    ],
    interfaces: [
      {
        name: UPLINK,
        mac: "9c:6b:00:bd:fe:41",
        mtu: 1500,
        state: "UP",
        kind: "device",
      },
      {
        name: "enp3s0f1np1",
        mac: "9c:6b:00:bd:fe:42",
        mtu: 1500,
        state: "DOWN",
        kind: "device",
      },
    ],
    routes: [
      {
        family: "inet6",
        dest: `${HOST_V6_PREFIX.replace(":15", ":743")}::/64`,
        via: null,
        dev: UPLINK,
        table: "main",
      },
      {
        family: "inet6",
        dest: "default",
        via: "fe80::262a:4ff:fec0:ffb7",
        dev: UPLINK,
        table: "main",
      },
      {
        family: "inet",
        dest: "default",
        via: "51.159.202.1",
        dev: UPLINK,
        table: "main",
      },
      ...VMS.filter((v) => v.state === "Running").map((v) => ({
        family: "inet6",
        dest: `${v.ipv6}/128`,
        via: null,
        dev: v.host_veth,
        table: "main",
      })),
    ],
    neigh_proxy: VMS.filter((v) => v.state === "Running").map((v) => ({
      address: v.ipv6,
      dev: UPLINK,
    })),
    ip_rules: [],
    reserved_ips: PROXY.reserved_ipv4
      ? [
          {
            address: PROXY.reserved_ipv4,
            attached_vm: PROXY.uuid,
            anchor: null,
            anchor_gateway: null,
            guest_ipv4: PROXY.ipv4_guest,
          },
        ]
      : [],
    proxy_maps: buildProxyMaps(VMS),
    private_mesh: VMS.filter((v) => v.private_ipv6).map((v) => ({
      address: v.private_ipv6,
      attached_vm: v.uuid,
      device: "atlas-mesh",
    })),
    migrations: VMS.filter((v) => v.migrating).map((v) => ({
      unit: `atlas-mig6-19657.service`,
      state: "running",
      peer: "51.159.110.51:19657",
    })),
    nft_tables: buildNft(VMS),
    units: [
      ...VMS.map((v) => ({
        name: `firecracker-vm@${v.uuid}.service`,
        load: "loaded",
        active: v.state === "Running" ? "active" : "inactive",
        sub:
          v.state === "Running"
            ? "running"
            : v.state === "Failed"
            ? "failed"
            : "dead",
        kind: "vm",
        description: `Atlas Firecracker VM ${v.uuid}`,
      })),
      {
        name: "atlas-mig6-19657.service",
        load: "loaded",
        active: "active",
        sub: "running",
        kind: "migration-forwarder",
        description: "socat TUN mig6-9ae90d69 <-> TCP:51.159.110.51:19657",
      },
      {
        name: "atlas-pool.service",
        load: "loaded",
        active: "active",
        sub: "exited",
        kind: "pool",
        description: "Atlas LVM thin pool",
      },
      {
        name: "nftables.service",
        load: "loaded",
        active: "active",
        sub: "exited",
        kind: "firewall",
        description: "nftables ruleset",
      },
    ],
    tasks: [
      {
        id: "t-8891",
        name: "provision-vm",
        status: "success",
        started_at: "2026-07-04T11:52:10Z",
        duration: "3m12s",
      },
      {
        id: "t-8890",
        name: "proxy-sync",
        status: "success",
        started_at: "2026-07-04T11:40:03Z",
        duration: "1.4s",
      },
      {
        id: "t-8889",
        name: "migrate-vm",
        status: "running",
        started_at: "2026-07-04T11:58:44Z",
        duration: null,
      },
    ],
    disks: [
      {
        name: "nvme0n1",
        kind: "disk",
        size: "953g",
        mount: null,
        model: "Samsung PM9A3",
        rota: 0,
      },
      {
        name: "nvme1n1",
        kind: "disk",
        size: "953g",
        mount: null,
        model: "Samsung PM9A3",
        rota: 0,
      },
      {
        name: "md2",
        kind: "raid1",
        size: "953g",
        mount: null,
        model: null,
        rota: 0,
      },
    ],
    users: [
      { name: "root", uid: 0, shell: "/bin/bash", home: "/root", sudo: true },
      {
        name: "atlas",
        uid: 1000,
        shell: "/bin/bash",
        home: "/home/atlas",
        sudo: true,
      },
    ],
    processes: VMS.filter((v) => v.state === "Running")
      .slice(0, 6)
      .map((v, i) => ({
        pid: 4000 + i,
        user: `atlas-fc-${v.fc_uid}`,
        rss: `${between(120, 900)}m`,
        kind: "firecracker",
        vm: v.uuid,
      })),
    metrics: buildMetrics(VMS, store.pool.data_percent),
  };
}

// ── run ───────────────────────────────────────────────────────────────────────
const wanted = process.argv.slice(2);
const names = wanted.length ? wanted : Object.keys(PROFILES);
for (const name of names) {
  const profile = PROFILES[name];
  if (!profile) {
    console.error(
      `unknown profile "${name}" — choose from: ${Object.keys(PROFILES).join(
        ", "
      )}`
    );
    process.exitCode = 1;
    continue;
  }
  const state = buildState(profile);
  const out = resolve(HERE, profile.out);
  writeFileSync(out, JSON.stringify(state, null, 2) + "\n");
  const VMS = state.virtual_machines;
  const committedVcpu = VMS.reduce((s, v) => s + v.vcpus, 0);
  const committedMem = VMS.reduce((s, v) => s + v.mem_mib, 0);
  console.log(`wrote ${out}`);
  console.log(
    `  ${VMS.length} VMs · committed ${committedVcpu} vCPU / ${committedMem} MiB`
  );
  console.log(
    `  physical ${CPU_TOTAL} vCPU / ${MEM_TOTAL_MIB} MiB · budget ×${OVERPROVISION} = ${
      CPU_TOTAL * OVERPROVISION
    } vCPU / ${MEM_TOTAL_MIB * OVERPROVISION} MiB`
  );
  console.log(`  pool fill ${state.pool.data_percent}%`);
}
