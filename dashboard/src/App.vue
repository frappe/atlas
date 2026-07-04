<template>
	<!-- The dashboard root. The base typography (Inter sans, relaxed line-height,
	     the neutral ink colour) is set here and inherited; every child styles
	     itself with Tailwind utilities off the frappe-ui preset. -->
	<div
		class="atlas-ui bg-surface-base text-ink-gray-7 font-sans text-base leading-[1.55] tracking-[-0.006em] antialiased"
	>
		<!-- Narrow content column tuned for a MacBook Air (capped 1080 so line-lengths
		     stay scannable and the F-pattern holds; fluid below). The whole app is one
		     viewport-tall column that never scrolls: header + footer are fixed bands,
		     the body flexes to fill the middle, overflow is handled INSIDE the panel. -->
		<div
			class="w-[min(100%-56px,1080px)] mx-auto h-screen pt-10 flex flex-col overflow-hidden"
		>
			<!-- ── Header: the host is the subject — hostname ONLY (item 7). The
			     running/pool/reserved summary moved to a one-line lede on the Overview
			     page; it informs once, it doesn't belong in the persistent chrome.
			     Right cluster is just what's actionable: the alerts summary (crit ·
			     warn · dead folded in) + the theme toggle. Refresh is implicit — reload
			     the browser. The cluster is width-locked so it can't shift when the
			     theme word (light/dark/system) changes length. ── -->
			<header class="flex items-center justify-between gap-6 mb-8 flex-none">
				<div>
					<h1
						class="m-0 font-mono tabular-nums text-2xl font-medium text-ink-gray-9 tracking-normal"
						:title="hostProvenance"
					>
						{{ state?.host?.hostname || "atlas host" }}
					</h1>
				</div>
				<div class="flex items-center justify-end gap-3.5 whitespace-nowrap">
					<!-- Alerts live on the Alerts page (and the rail carries the firing
					     count); the header no longer duplicates crit/warn/dead. Only the
					     theme toggle remains actionable here. A fixed-width glyph slot
					     (○/●/◐) so switching modes never shifts the cluster. -->
					<button
						class="bg-transparent border-0 p-0 cursor-pointer w-4 text-center text-base leading-none text-ink-gray-5 hover:text-ink-gray-8 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
						:title="`Theme: ${mode} — click to switch`"
						@click="cycleMode"
					>
						{{ themeGlyph }}
					</button>
				</div>
			</header>

			<p v-if="error" class="text-sm text-ink-gray-6">
				Could not read host state. {{ error }}
			</p>

			<!-- body: rail + panel. Fill the middle of the viewport column and clip:
			     the panel owns any overflow so the page itself never scrolls. Below
			     640px the rail becomes a top strip. -->
			<div
				v-if="state"
				class="grid grid-cols-1 sm:grid-cols-[10rem_1fr] flex-1 min-h-0 overflow-hidden"
			>
				<div
					class="pb-3 mb-5 border-b border-outline-gray-1 sm:pb-0 sm:mb-0 sm:pr-[14px] sm:border-b-0 overflow-y-auto"
				>
					<!-- Two-level rail: every object gets its own line. -->
					<Rail
						:domains="domains"
						:domain="selected.domain"
						:table="selected.table"
						@select="select"
					/>
				</div>

				<!-- panel. Left padding is the rail gutter (no divider). Fallback scroll
				     for a panel that genuinely can't fit; clip-x contains the inner
				     .scroll's -12px bleed so no screen ever scrolls sideways. -->
				<main class="min-w-0 pl-0 sm:pl-7 overflow-y-auto overflow-x-hidden flex flex-col">
					<!-- Overview — the status/scale layer (landing). Four sub-pages:
					     Summary (quota bars + histogram + wants-a-look), Alerts (the
					     stateful list), Analytics (deep view), Events (the log). -->
					<template v-if="selected.domain === 'overview'">
						<!-- No count on the Alerts page head: the rail Alerts sub-item
						     carries the firing number, and the list itself enumerates them.
						     (item: "alerts count shows up in 3 places".) -->
						<PanelHead :title="overviewTitle" />
						<Overview
							v-if="selected.table === 'summary'"
							:state="state"
							@open-vm="openMachine"
							@open-alerts="select({ domain: 'overview', table: 'alerts' })"
						/>
						<AlertsList
							v-else-if="selected.table === 'alerts'"
							:model="alertModel"
							:clear-line="nominalLine"
							paginate
							@open-vm="openMachine"
						/>
						<Analytics v-else-if="selected.table === 'analytics'" :state="state" />
						<!-- Events — one chronological log, folded together. A plain ListView
						     section like any other: Time and Kind are quiet mono/uppercase
						     leads (col.class), the Event sentence grows to fill the row. -->
						<ListView
							v-else-if="selected.table === 'events'"
							:columns="eventCols"
							:rows="eventRows"
							:row-px="35"
							:reserve="340"
							empty-text="No recent events."
						/>
					</template>

					<!-- Machines — the volatile subject. VmTable owns its own header
					     (title, live filtered count, search + facet chips) and opens a
					     selected VM in a bottom dock rather than expanding a row, so the
					     page never scrolls. -->
					<VmTable
						v-else-if="selected.table === 'machines'"
						:state="state"
						:vms="vms"
						:uplink="uplink"
						:open-vm="openVm"
						@open-image="select({ domain: 'images', table: 'images' })"
					/>

					<!-- Firewall — the ruleset flattened into a paginated list so a
					     host with thousands of per-VM rules fits one screen. Rendered as
					     a normal fill table (the Match column grows to absorb the width,
					     like every other section) so it spreads across the panel instead
					     of packing left. Per-VM rules keep their → uuid8 back-link; the
					     full raw rule is one hover away. -->
					<ListView
						v-else-if="selected.table === 'nftables'"
						title="Firewall"
						:columns="fwCols"
						:rows="fwRules"
						:backlink="(r) => r.vm || null"
						:row-px="31"
						:reserve="400"
						@open-vm="openMachine"
					>
						<template #strip>
							<!-- One quiet mono line naming the tables + their chains, so it
							     reads like every other section's calm summary. -->
							<p
								v-if="fwTables.length"
								class="-mt-1 mb-3.5 flex-none text-2xs text-ink-gray-6 whitespace-normal leading-relaxed font-mono tabular-nums"
							>
								<span v-for="(t, i) in fwTables" :key="t.name">
									<template v-if="i"> · </template>{{ t.family }} {{ t.name
									}}<span class="text-ink-gray-5"> ({{ t.chainNames }})</span>
								</span>
							</p>
						</template>
					</ListView>

					<!-- Storage → Analytics — the LVM stack (PV → VG → pool) chart. Its
					     own bespoke branch: a chart, not a table. Volumes (the LVs) is a
					     plain ListView section reached through the generic path below. -->
					<StorageAnalytics
						v-else-if="selected.table === 'storage-analytics'"
						:state="state"
					/>

					<!-- Every other object is a single ListView — one at a time,
					     reached from its own rail line. Cross-links wired per object. -->
					<ListView
						v-else-if="activeSection"
						:key="selected.table"
						:title="activeSection.label"
						:columns="activeSection.columns"
						:rows="activeSection.rows"
						:summary="activeSection.summary"
						:key-col="activeSection.keyCol"
						:backlink="activeSection.backlink"
						:vm-filter="activeSection.vmFilter"
						@open-vm="openMachine"
					/>
				</main>
			</div>

			<!-- Footer: just the collection timestamp — the single provenance fact
			     worth a persistent band. The host build line (OS · kernel · Firecracker
			     · arch) is reference-only, so it lives on hover over the hostname rather
			     than taking a footer row of its own. -->
			<footer
				v-if="state"
				class="flex-none flex items-baseline justify-end py-3.5 text-xs text-ink-gray-6 whitespace-nowrap overflow-hidden"
			>
				<span class="font-mono tabular-nums text-ink-gray-6">{{ collectedAt }}</span>
			</footer>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import Rail from "./components/Rail.vue";
import VmTable from "./components/VmTable.vue";
import ListView from "./components/ListView.vue";
import PanelHead from "./components/PanelHead.vue";
import Overview from "./components/Overview.vue";
import StorageAnalytics from "./components/StorageAnalytics.vue";
import Analytics from "./components/Analytics.vue";
import AlertsList from "./components/AlertsList.vue";
import {
	vmByRule,
	alerts,
	events,
	parseNftRule,
	uuid8,
	pct,
	shortTime,
	fmtGiB,
} from "./derive.js";
import { mode, cycleMode } from "./theme.js";

const themeGlyph = computed(() => ({ light: "○", dark: "●", system: "◐" }[mode.value]));

const state = ref(null);
const error = ref("");
// The page opens on the Overview summary — health first (the scale-layer landing).
// {domain, table} so the rail can drive one object at a time.
const selected = ref({ domain: "overview", table: "summary" });
const openVm = ref(null); // set by a section back-link to open a VM inline

// ── the status/scale layer (Overview domain) ──
const alertModel = computed(() =>
	state.value ? alerts(state.value) : { firing: [], cleared: [] }
);
const firingCount = computed(() => alertModel.value.firing.length);
const nominalLine = computed(() => {
	const running = vms.value.filter((v) => v.state === "Running").length;
	return `${running} of ${vms.value.length} running nominally. Nothing wants a look.`;
});
const eventRows = computed(() => (state.value ? events(state.value) : []));
const eventsCount = computed(() => eventRows.value.length);
const overviewTitle = computed(
	() =>
		({ summary: "Overview", alerts: "Alerts", analytics: "Analytics", events: "Events" }[
			selected.value.table
		] || "Overview")
);
async function load() {
	error.value = "";
	try {
		// Preserve the full search string so the ?src= source selector is kept.
		// VITE_API_BASE lets dev point at the SSH proxy (backend/proxy.py) for live
		// real-host data instead of the checked-in fixtures; empty = same origin.
		const base = import.meta.env.VITE_API_BASE || "";
		const res = await fetch(base + "/api/state" + window.location.search, {
			cache: "no-store",
		});
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		state.value = await res.json();
	} catch (e) {
		error.value = String(e.message || e);
	}
}
onMounted(load);

function select({ domain, table }) {
	selected.value = { domain, table: table || domainDefaultTable(domain) };
}
function domainDefaultTable(domain) {
	return domains.value.find((d) => d.id === domain)?.tables?.[0]?.id;
}
// Cross-link: switch to Machines and open the given VM inline.
function openMachine(uuid) {
	if (!uuid) return;
	selected.value = { domain: "machines", table: "machines" };
	// Re-assign so VmTable's watcher fires even if the same VM is requested twice.
	openVm.value = null;
	requestAnimationFrame(() => (openVm.value = uuid));
}

// ── flat slices ──
const vms = computed(() => state.value?.virtual_machines || []);
const images = computed(() => state.value?.images || []);
const snapshots = computed(() => state.value?.snapshots || []);
const addresses = computed(() => state.value?.addresses || []);
const routes = computed(() => state.value?.routes || []);
const reservedIps = computed(() => state.value?.reserved_ips || []);
const neighProxy = computed(() => state.value?.neigh_proxy || []);
const ipRules = computed(() => state.value?.ip_rules || []);
const nftTables = computed(() => state.value?.nft_tables || []);
const units = computed(() => state.value?.units || []);
const packages = computed(() => state.value?.host?.packages || []);
const uplink = computed(() => state.value?.host?.uplink || "eth0");
// Plan A new slices.
const volumes = computed(() => state.value?.volumes || []);
const proxyMaps = computed(() => state.value?.proxy_maps || []);
const privateMesh = computed(() => state.value?.private_mesh || []);
const migrationsRows = computed(() => state.value?.migrations || []);
const tasks = computed(() => state.value?.tasks || []);
const disks = computed(() => state.value?.disks || []);
const usersRows = computed(() => state.value?.users || []);
const processes = computed(() => state.value?.processes || []);

// Interfaces need a renamed state key so the ListView slot doesn't collide.
const ifaceRows = computed(() =>
	(state.value?.interfaces || []).map((i) => ({ ...i, iface_state: i.state }))
);

const idx = computed(() => (state.value ? vmByRule(state.value) : {}));

// ── header ──
// The host summary line moved to the Overview lede (item 7); the header is just
// the hostname.
const machinesSummary = computed(() => {
	const running = vms.value.filter((v) => v.state === "Running").length;
	return `${running} running · ${vms.value.length - running} stopped`;
});
const unitsSummary = computed(() => {
	const up = units.value.filter((u) => u.active === "active").length;
	return units.value.length ? `${up} up · ${units.value.length - up} dead` : "";
});
const volumesSummary = computed(() => {
	const n = volumes.value.length;
	if (!n) return "";
	const vmDisks = volumes.value.filter((v) => v.role === "vm-disk").length;
	return `${vmDisks} VM disk${vmDisks === 1 ? "" : "s"}`;
});
const nftRuleCount = computed(() =>
	nftTables.value.reduce((n, t) => n + t.chains.reduce((m, c) => m + c.rules.length, 0), 0)
);
const nftSummary = computed(() => {
	const n = nftTables.value.length;
	if (!n) return "";
	return `${n} ${n === 1 ? "table" : "tables"} · ${nftRuleCount.value} rules`;
});

// ── Firewall view (rendered through ListView) ──
// The structure strip: each table with its chains, quiet, above the flat list.
const fwTables = computed(() =>
	nftTables.value.map((t) => ({
		name: t.name,
		family: t.family,
		persisted: t.persisted,
		chainNames: t.chains.map((c) => c.name).join(" · "),
	}))
);
// Every rule flattened + PARSED (item 15) into scannable columns: the chain
// context, then verdict · match · iface · to, plus the → VM back-link. The raw
// string is kept as a title so the full rule is one hover away.
const fwRules = computed(() => {
	const out = [];
	for (const t of nftTables.value) {
		for (const c of t.chains) {
			for (const rule of c.rules) {
				const p = parseNftRule(rule);
				out.push({
					chain: c.name,
					verdict: p.verdict,
					match: p.match,
					iface: p.iface,
					to: p.to,
					raw: p.raw,
					vm: idx.value.rule(rule),
				});
			}
		}
	}
	return out;
});
// Match grows to absorb the panel width (fill mode) so the table spreads like
// every other section; Interface and To follow and pin toward the right. The raw
// rule text is carried on hover via the parsed fields, so no per-cell truncation
// is needed here.
const fwCols = [
	{ key: "chain", label: "Chain", mono: true },
	{ key: "verdict", label: "Action" },
	{ key: "match", label: "Match", mono: true, grow: true },
	{ key: "iface", label: "Interface", mono: true },
	{ key: "to", label: "To", mono: true },
];

const collectedAt = computed(() => {
	const t = state.value?.host?.collected_at;
	return t ? t.replace("T", " ").replace("Z", " UTC") : "—";
});

// The host build line — reference only, surfaced on hover over the hostname
// rather than a persistent footer row. Order: OS · Linux <kernel> · Firecracker
// · arch, matching the old footer.
const hostProvenance = computed(() => {
	const h = state.value?.host;
	if (!h) return "";
	return [
		h.linux,
		`Linux ${h.kernel_version}`,
		`Firecracker ${h.firecracker_version}`,
		h.architecture,
	]
		.filter(Boolean)
		.join(" · ");
});

// ── column defs ──
// `grow: true` on a descriptive column makes the table fill the panel width (the
// column absorbs the slack) rather than packing left with dead space. Reserved
// for tables with a genuinely variable-length column; all-short-token reference
// tables (IP rules) stay left-packed.
const imageCols = [
	{ key: "name", label: "Image", grow: true },
	{ key: "kernel", label: "Kernel", mono: true },
	{ key: "rootfs_size", label: "Rootfs", mono: true, align: "right" },
	{ key: "base_lv_size", label: "Base LV", mono: true, align: "right" },
];
const snapCols = [
	{ key: "uuid", label: "UUID", mono: true, format: uuid8 },
	{ key: "kind", label: "Kind" },
	{ key: "origin_lv", label: "Origin LV", mono: true, grow: true },
	{ key: "data_percent", label: "Data %", align: "right", format: pct },
];
const addrCols = [
	{ key: "interface", label: "Interface", mono: true },
	{ key: "family", label: "Family", mono: true },
	{ key: "address", label: "Address", mono: true, grow: true },
	{ key: "scope", label: "Scope" },
];
const ifaceCols = [
	{ key: "name", label: "Name", mono: true, grow: true },
	{ key: "iface_state", label: "State", status: "UP" },
	{ key: "kind", label: "Kind" },
	{ key: "mac", label: "MAC", mono: true },
	{ key: "mtu", label: "MTU", mono: true, align: "right" },
];
const routeCols = [
	{ key: "family", label: "Family", mono: true },
	{ key: "dest", label: "Destination", mono: true, grow: true },
	{ key: "via", label: "Via", mono: true },
	{ key: "dev", label: "Device", mono: true },
];
// Reserved keeps its attached-VM value; it renders in the back-link column.
const ripCols = [
	{ key: "address", label: "Address", mono: true, grow: true },
	{ key: "guest_ipv4", label: "Guest v4", mono: true },
];
const ndpCols = [
	{ key: "address", label: "Address", mono: true, grow: true },
	{ key: "dev", label: "Device", mono: true },
];
const ruleCols = [
	{ key: "priority", label: "Priority", mono: true, align: "right" },
	{ key: "from", label: "From", mono: true },
	{ key: "table", label: "Table", mono: true },
];
const unitCols = [
	{ key: "name", label: "Unit", mono: true, grow: true },
	// A unit reads "<active> · <sub>" (e.g. "active · running"), the sub carried
	// on the row — expressed here as the column's format so it's visible in the
	// def, not hidden in the primitive.
	{
		key: "active",
		label: "State",
		status: "active",
		format: (v, row) => (row.sub ? `${v} · ${row.sub}` : v),
	},
	{ key: "kind", label: "Kind" },
];
const pkgCols = [
	{ key: "name", label: "Package", grow: true },
	{ key: "version", label: "Version", mono: true },
];
// Events — Time and Kind are quiet mono/uppercase leads (col.class); the Event
// sentence grows. All shaping is on the column def, so the log needs no wrapper.
const eventCols = [
	{
		key: "at",
		label: "Time",
		class: "text-2xs text-ink-gray-5 font-mono tabular-nums",
		format: (v) => shortTime(v),
	},
	{ key: "kind", label: "Kind", class: "text-2xs uppercase tracking-wider text-ink-gray-5" },
	{ key: "text", label: "Event", grow: true, class: "text-ink-gray-7" },
];
// Storage → Volumes — the LVs as a plain section. Name grows; size reads from the
// _bytes field via fmtGiB, data_percent through the shared rounded percent.
const volCols = [
	{ key: "name", label: "Volume", mono: true, grow: true },
	{ key: "role", label: "Role" },
	{
		key: "size",
		label: "Size",
		mono: true,
		align: "right",
		format: (v, row) => fmtGiB(row.size_bytes),
	},
	{ key: "origin", label: "Origin", mono: true },
	{ key: "data_percent", label: "Data %", mono: true, align: "right", format: pct },
];

// ── 20-A: host-only row filters ──
// Units / Interfaces / Processes each carry a per-VM tail (a firecracker unit, a
// tap/veth iface, a firecracker process) that swamps the host primitives at scale
// (1000 VMs → 1000 tap ifaces). Default to host-only; ListView folds the VM
// rows behind a toggle. B's hook: `isBroken` rows are never hidden — a dead unit
// or downed tap always surfaces so you can see the broken part.
const unitFilter = {
	isVm: (r) => r.kind === "vm",
	isBroken: (r) => r.active !== "active",
	noun: "VM units",
};
const ifaceFilter = {
	isVm: (r) => r.kind === "tap" || r.kind === "veth",
	// A VM iface is broken when it's not UP (a downed tap = a VM with no network).
	isBroken: (r) => r.iface_state !== "UP",
	noun: "VM interfaces",
};
const procFilter = {
	// A per-VM process is one bound to a VM (firecracker child carries the uuid).
	isVm: (r) => !!r.vm || r.kind === "firecracker",
	// Processes expose no health field — none are "broken"; nothing forced-visible.
	isBroken: () => false,
	noun: "VM processes",
};
// ── Network extensions (Plan A) ──
const proxyCols = [
	{ key: "listen", label: "Listen", mono: true },
	{ key: "protocol", label: "Protocol" },
	{ key: "backend", label: "Backend", mono: true },
	{ key: "sni", label: "SNI", mono: true, grow: true },
];
const meshCols = [
	{ key: "address", label: "Private /128", mono: true, grow: true },
	{ key: "device", label: "Device", mono: true },
];
const migCols = [
	{ key: "unit", label: "Forwarder", mono: true, grow: true },
	{ key: "state", label: "State" },
	{ key: "peer", label: "Peer", mono: true },
];
// ── System extensions (Plan A) ──
const taskCols = [
	{ key: "name", label: "Task", grow: true },
	{ key: "status", label: "Status" },
	{ key: "started_at", label: "Started", mono: true },
	{ key: "duration", label: "Duration", mono: true, align: "right" },
];
const diskCols = [
	{ key: "name", label: "Device", mono: true },
	{ key: "kind", label: "Kind" },
	{ key: "size", label: "Size", mono: true, align: "right" },
	{ key: "mount", label: "Mount", mono: true },
	{ key: "model", label: "Model", grow: true },
];
const userCols = [
	{ key: "name", label: "User", mono: true },
	{ key: "uid", label: "UID", mono: true, align: "right" },
	{ key: "sudo", label: "Sudo" },
	{ key: "shell", label: "Shell", mono: true, grow: true },
];
// 19: PID leads LEFT-aligned (it's an identifier, not a magnitude) so the first
// column hugs the panel edge instead of hugging the right of a narrow column —
// that right-aligned-first-column was the "big gap after the sidebar". Only RSS,
// a real magnitude, stays right-aligned with tabular figures.
const procCols = [
	{ key: "pid", label: "PID", mono: true },
	{ key: "kind", label: "Kind", grow: true },
	{ key: "user", label: "User", mono: true },
	{ key: "rss", label: "RSS", mono: true, align: "right" },
];

// An interface belongs to a VM when its name/MAC carries a VM token (tap/veth).
function ifaceOwner(row) {
	return idx.value.any ? idx.value.any(row.name) || idx.value.any(row.mac) : null;
}

// ── domain model — every object is its own table (its own rail line) ──
// Ordered by volatility: what changes minute-to-minute first, static last.
const domains = computed(() => {
	const s = state.value;
	if (!s) return [];

	// `alert` is the ONLY count the rail shows (item: hide numbers except
	// actionables). A domain's alert is the max of its tables' alerts.
	const D = (id, label, tables, { noAlert = false } = {}) => ({
		id,
		label,
		count: tables.reduce((n, t) => n + t.count, 0),
		// System rolls up host primitives (dead units live on its Units sub-item);
		// the domain header itself stays a bare label — the count belongs on the
		// sub-item, not the rail's top level.
		alert: noAlert ? null : tables.reduce((n, t) => n + (t.alert || 0), 0) || null,
		tables,
	});
	const T = (id, label, columns, rows, extra = {}) => ({
		id,
		label,
		columns,
		rows,
		count: rows.length,
		alert: extra.alert || null,
		summary: extra.summary || "",
		keyCol: extra.keyCol || null,
		backlink: extra.backlink || null,
		vmFilter: extra.vmFilter || null,
	});

	// A marker table — rendered by a bespoke panel branch (Overview, Machines,
	// Firewall), not by ListView. Carries id/label/count (+ optional alert).
	const M = (id, label, count, alert = null) => ({
		id,
		label,
		columns: [],
		rows: [],
		count,
		alert,
	});

	return [
		// The status/scale layer. Landing domain, first line. Its rail signal is the
		// firing-alert total — the only actionable number in the nav.
		// The firing-alert count lives on ONE rail line — the Alerts sub-item — not
		// also rolled up onto the Overview domain (item: "alerts count shows up in 3
		// places"). noAlert keeps the domain header a bare label.
		D(
			"overview",
			"Overview",
			[
				M("summary", "Summary", vms.value.length),
				M("alerts", "Alerts", firingCount.value, firingCount.value || null),
				M("analytics", "Analytics", vms.value.length),
				M("events", "Events", eventsCount.value),
			],
			{ noAlert: true }
		),
		D("machines", "Machines", [
			T("machines", "Machines", [], vms.value, { summary: machinesSummary.value }),
		]),
		D("images", "Images", [
			T("images", "Images", imageCols, images.value),
			T("snapshots", "Snapshots", snapCols, snapshots.value, {
				// A snapshot links to the live VM it was taken from, when one exists.
				backlink: (r) =>
					r.uuid && vms.value.some((v) => v.uuid === r.uuid) ? r.uuid : null,
			}),
		]),
		// Storage — the LVM domain, split into two rail lines: Volumes (a plain
		// ListView section, the LVs) and Analytics (the PV→VG→pool stack chart,
		// rendered by its own bespoke branch — a chart, not a table).
		D("storage", "Storage", [
			T("volumes", "Volumes", volCols, volumes.value, {
				summary: volumesSummary.value,
				// A vm-disk volume links back to its VM (name carries the uuid).
				backlink: (r) => idx.value.any(r.name),
			}),
			M("storage-analytics", "Analytics", volumes.value.length),
		]),
		D("network", "Network", [
			T("addresses", "Addresses", addrCols, addresses.value, { keyCol: "address" }),
			T("interfaces", "Interfaces", ifaceCols, ifaceRows.value, {
				backlink: ifaceOwner,
				vmFilter: ifaceFilter,
			}),
			T("routes", "Routes", routeCols, routes.value, { backlink: idx.value.route }),
			T("reserved", "Reserved IPs", ripCols, reservedIps.value, {
				keyCol: "address",
				backlink: idx.value.reserved,
			}),
			// Proxy & TCP (Plan A) → "Proxy maps" sub-table.
			T("proxy", "Proxy maps", proxyCols, proxyMaps.value, {
				keyCol: "listen",
				backlink: (r) => r.vm || null,
			}),
			// VPN / private mesh (Plan A) → "Private mesh" sub-table.
			T("mesh", "Private mesh", meshCols, privateMesh.value, {
				keyCol: "address",
				backlink: (r) => r.attached_vm || null,
			}),
			T("ndp", "Proxy NDP", ndpCols, neighProxy.value, { backlink: idx.value.ndp }),
			T("rules", "IP rules", ruleCols, ipRules.value, { backlink: idx.value.ipRule }),
			// Migration (Plan A) → "Migrations" sub-table (VM row carries the flag).
			T("migrations", "Migrations", migCols, migrationsRows.value, {
				backlink: (r) => idx.value.any(r.unit),
			}),
		]),
		D("firewall", "Firewall", [
			{
				id: "nftables",
				label: "Firewall",
				columns: [],
				rows: [],
				count: nftRuleCount.value,
				summary: nftSummary.value,
			},
		]),
		// System (Plan A, renamed) — the host primitives beside Units/Packages:
		// host-side Tasks, Disks, Users, Processes.
		D(
			"system",
			"System",
			[
				T("units", "Units", unitCols, units.value, {
					backlink: idx.value.unit,
					vmFilter: unitFilter,
				}),
				T("tasks", "Tasks", taskCols, tasks.value),
				T("disks", "Disks", diskCols, disks.value),
				T("users", "Users", userCols, usersRows.value, { keyCol: "name" }),
				T("processes", "Processes", procCols, processes.value, {
					backlink: (r) => r.vm || null,
					vmFilter: procFilter,
				}),
				T("packages", "Packages", pkgCols, packages.value),
			],
			{ noAlert: true }
		),
	];
});

// The single object table currently selected (null for the Overview sub-pages,
// machines and nftables, which render their own bespoke panels above).
const BESPOKE = new Set([
	"machines",
	"nftables",
	"storage-analytics",
	"summary",
	"alerts",
	"analytics",
	"events",
]);
const activeSection = computed(() => {
	if (selected.value.domain === "overview") return null;
	const d = domains.value.find((d) => d.id === selected.value.domain);
	if (!d) return null;
	const t = d.tables.find((t) => t.id === selected.value.table) || d.tables[0];
	if (!t || BESPOKE.has(t.id)) return null;
	return t;
});
</script>
