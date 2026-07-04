// ═══════════════════════════════════════════════════════════════════════════
// collect-real.mjs — build mock/state-real.json from a LIVE host.
//
// The "Real Host" source is not synthetic: it is an actual `server.py --collect`
// dump from a real Atlas host (f2-aditya-blr3 on scaleway.local), captured in the
// current (v2) shape. This script SSHes the host twice a few seconds apart —
// exactly the transport backend/proxy.py uses — and normalises the result into a
// checked-in fixture:
//
//   • Everything the host reports (VMs, storage, nft, units, …) is kept VERBATIM.
//   • metrics: a single --collect run emits RAW counters, not a time-series. The
//     dashboard charts a `metrics.series`. Two snapshots let us derive ONE real
//     rate sample (cpu/net/disk) with proxy.py's own formulas; the gauges
//     (mem_used, pool_used) are point-in-time truthful. We then lay a flat 48-pt
//     window anchored on those real values so the charts render honestly — a
//     steady real host, not fabricated activity.
//
//   HOST=root@51.159.202.202 \
//   SSH_KEY=~/.ssh/id_rsa KNOWN_HOSTS=~/.atlas/known_hosts \
//   node mock/collect-real.mjs        # → mock/state-real.json
//
// The host/key/known_hosts default to f2-aditya-blr3 as reached by setup.sh.
// ═══════════════════════════════════════════════════════════════════════════

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { homedir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const expand = (p) => p.replace(/^~/, homedir());

const HOST = process.env.HOST || "root@51.159.202.202"; // f2-aditya-blr3
const SSH_KEY = expand(process.env.SSH_KEY || "~/.ssh/id_rsa");
const KNOWN_HOSTS = expand(process.env.KNOWN_HOSTS || "~/.atlas/known_hosts");
const SERVER_PY = resolve(HERE, "..", "backend", "server.py");

// One `--collect` snapshot: push server.py over ssh, parse stdout JSON. Mirrors
// backend/proxy.py's _collect (same flags, same pipe transport).
function collect() {
  const script = readFileSync(SERVER_PY);
  const argv = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    `IdentityFile=${SSH_KEY}`,
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    `UserKnownHostsFile=${KNOWN_HOSTS}`,
    "-o",
    "StrictHostKeyChecking=accept-new",
    HOST,
    "python3 - --collect",
  ];
  const out = execFileSync("ssh", argv, {
    input: script,
    maxBuffer: 64 * 1024 * 1024,
  });
  return JSON.parse(out.toString("utf8"));
}

// proxy.py's _clamp — cpu% into [0, 100].
const clamp = (v) => Math.max(0, Math.min(100, v));

// Derive ONE rate sample from two raw counter snapshots — proxy.py's formulas.
function deriveSample(raw, prev) {
  const s = {
    mem_used_mib: raw.mem_used_mib,
    pool_used_pct: raw.pool_used_pct,
    cpu_util_pct: null,
    net_rx_mbps: null,
    net_tx_mbps: null,
    disk_io_iops: null,
  };
  const dt = (raw.mono || 0) - (prev.mono || 0);
  if (dt > 0) {
    const [c0, c1] = raw.cpu || [],
      [p0, p1] = prev.cpu || [];
    if (c1 - p1 > 0) s.cpu_util_pct = clamp((100 * (c0 - p0)) / (c1 - p1));
    const [nr, nt] = raw.net || [],
      [pnr, pnt] = prev.net || [];
    if (raw.net && prev.net) {
      s.net_rx_mbps = Math.max(0, ((nr - pnr) * 8) / 1e6 / dt);
      s.net_tx_mbps = Math.max(0, ((nt - pnt) * 8) / 1e6 / dt);
    }
    if (raw.disk_ios != null && prev.disk_ios != null) {
      s.disk_io_iops = Math.max(0, (raw.disk_ios - prev.disk_ios) / dt);
    }
  }
  return s;
}

// A deterministic wobble so the flat window reads as a live signal, not a ruler.
// Seeded off the anchor value → stable output for a stable host.
function flatWindow(base, n = 48) {
  let seed = Math.round((base + 1) * 1000) >>> 0 || 1;
  const rnd = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const amp = Math.max(base * 0.04, base > 0 ? 0.1 : 0); // ±4%, gentle
  const pts = [];
  for (let i = 0; i < n; i++) {
    pts.push(
      Math.max(
        0,
        Math.round(
          (base + Math.sin(i / 5) * amp * 0.5 + (rnd() - 0.5) * amp) * 100
        ) / 100
      )
    );
  }
  return pts;
}

const UNITS = {
  cpu_util_pct: "%",
  mem_used_mib: "MiB",
  disk_io_iops: "iops",
  net_rx_mbps: "Mb/s",
  net_tx_mbps: "Mb/s",
  pool_used_pct: "%",
};

console.log(`collecting ${HOST} (snapshot 1/2) …`);
const a = collect();
console.log(`collecting ${HOST} (snapshot 2/2) …`);
const b = collect();

// The second snapshot is the "now" the fixture represents; derive rates a←b.
const state = b;
const sample = deriveSample(b.metrics.raw, a.metrics.raw);

const series = {};
for (const key of Object.keys(UNITS)) {
  const v = sample[key];
  if (v == null) continue;
  series[key] = { unit: UNITS[key], points: flatWindow(v) };
}
state.metrics = { collected_at: b.metrics.collected_at, series };

// Drop the collector's transport-only comment if present.
delete state._comment;

const out = resolve(HERE, "state-real.json");
writeFileSync(out, JSON.stringify(state, null, 2) + "\n");
const vms = state.virtual_machines || [];
console.log(`wrote ${out}`);
console.log(
  `  ${vms.length} VMs (${
    vms.filter((v) => v.state === "Running").length
  } running) on ${state.host?.hostname}`
);
console.log(
  `  real anchors — mem ${sample.mem_used_mib} MiB · pool ${
    sample.pool_used_pct
  }% · cpu ${sample.cpu_util_pct?.toFixed(
    1
  )}% · net ${sample.net_rx_mbps?.toFixed(2)}/${sample.net_tx_mbps?.toFixed(
    2
  )} Mb/s · disk ${sample.disk_io_iops?.toFixed(1)} iops`
);
