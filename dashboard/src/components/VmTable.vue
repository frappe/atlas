<template>
	<!-- The Machines centrepiece. The VM is the subject: a wide monochrome table
	     you scan and open. Two rules the previous version broke are now structural:

	     1. Opening a VM must NOT scroll the page. A selected row no longer expands
	        in place; instead the joined detail fills a fixed DOCK pinned to the
	        bottom of the panel. The table above flexes and paginates to whatever
	        height is left, so the whole Machines screen always fits one viewport.

	     2. Finding one VM among a thousand needs search — the filter lives on the
	        RIGHT of the panel header, through the SHARED filter that every
	        searchable list uses (a `filter` config; ListView owns the state).

	     The table body, header, filter toolbar + logic, pagination, dimming and
	     column machinery are all the shared ListView; this component only DECLARES
	     the Machines-specific filter (search fields + facet defs + count line),
	     the computed cell rendering (#cell), and the detail dock (#after). -->
	<ListView
		ref="table"
		class="relative"
		title="Machines"
		:filter="filter"
		:h3="false"
		:columns="cols"
		:rows="vms"
		:row-px="42"
		:reserve="320"
		fill
		spread
		:row-key-fn="(vm) => vm.uuid"
		:open-key="openUuid"
		:dim-fn="(vm) => vm.state !== 'Running'"
		clickable
		empty-text="No machines match this filter."
		@row-click="(vm) => toggle(vm.uuid)"
	>
		<!-- Custom cell rendering — the Machines table's cells are computed
		     (state word, prov tag, used/total splits), not plain values, so it
		     renders them itself through the shared column grid. The `dim` flag (a
		     stopped/paused VM) is passed in so the computed cells read a step
		     lighter, consistently with the shared row dimming. -->
		<template #cell="{ row: vm, col, dim }">
			<template v-if="col.key === 'uuid'">{{ uuid8(vm.uuid) }}</template>

			<span v-else-if="col.key === 'state'"
				>{{ stateWord(vm)
				}}<span v-if="prov(vm).kind" :class="dim ? 'text-ink-gray-5' : 'text-ink-gray-6'">
					· {{ prov(vm).kind }}</span
				></span
			>

			<UsedTotal
				v-else-if="col.key === 'cpu'"
				:used="prov(vm).cpuUsedText || null"
				:total="prov(vm).cpu"
				:used-ink="dim ? 'text-ink-gray-6' : 'text-ink-gray-8'"
			/>

			<UsedTotal
				v-else-if="col.key === 'mem'"
				:used="prov(vm).memUsedText || null"
				:total="prov(vm).mem"
				:used-ink="dim ? 'text-ink-gray-6' : 'text-ink-gray-8'"
			/>

			<span
				v-else-if="col.key === 'origin'"
				class="text-sm"
				:class="dim ? 'text-ink-gray-5' : 'text-ink-gray-6'"
				:title="origin(vm)"
				>{{ origin(vm) }}</span
			>
		</template>

		<!-- Detail overlay: the selected VM's joined detail. Floats ON TOP of the
		     bottom of the panel (absolute) so opening a VM never reflows the table
		     or the pager. One close control, top-right, aligned to the panel edge.
		     Opens instantly — no slide/fade; a read-only instrument shows, it
		     doesn't perform. -->
		<template #after>
			<div
				v-if="openVmObj"
				class="absolute -inset-x-3 bottom-0 z-[12] bg-surface-base border-t border-outline-gray-1 flex flex-col min-h-0"
			>
				<!-- Header row: uuid on the left, close ✕ on the right. Both live in
					     the SAME row so the title and the ✕ sit the same distance from the
					     top border and from their respective edges (px-3 = 12px). -->
				<div class="flex items-center gap-2.5 px-3 pt-3 pb-2.5 flex-none">
					<span class="font-mono tabular-nums text-ink-gray-9 text-xs">{{
						uuid8(openVmObj.uuid)
					}}</span>
					<button
						class="ml-auto -my-1 inline-flex items-center justify-center w-6 h-6 bg-transparent border-0 text-sm leading-none text-ink-gray-5 cursor-pointer p-0 rounded-sm hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
						aria-label="Close detail"
						@click="openUuid = null"
					>
						✕
					</button>
				</div>
				<div class="px-3 pb-4 min-h-0">
					<VmDetail
						:state="state"
						:vm="openVmObj"
						:uplink="uplink"
						@open-image="$emit('open-image', openVmObj)"
					/>
				</div>
			</div>
		</template>
	</ListView>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";
import VmDetail from "./VmDetail.vue";
import ListView from "./ListView.vue";
import UsedTotal from "./UsedTotal.vue";
import { diskOrigin, uuid8, perVmProvisioning } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
	vms: { type: Array, required: true },
	uplink: { type: String, default: "eth0" },
	// Cross-link entry: when a section back-link opens a VM, App.vue sets this.
	openVm: { type: String, default: null },
});
defineEmits(["open-image"]);

// Column defs for the shared table. Every cell is rendered via the #cell slot;
// `align`/`mono` still drive the shared cell classes. Five short-token columns
// with no single variable-length column to `grow`, so the table `spread`s them
// evenly across the panel (like Disks/Users) instead of packing them into the
// left third and stranding the whole right half of the panel empty.
// Five columns — the facts you scan to tell machines apart. The provisioning kind
// (warm/cold) folds into State as "running · warm". Three per-VM facts live in the
// detail dock instead of the scan table: Disk % and Ingress (read after you open a
// row, not while scanning 45), and Tenant is dropped entirely (the host can't know
// it — it was an empty column on every real host).
const cols = [
	{ key: "uuid", label: "UUID", mono: true },
	{ key: "state", label: "State", mono: true },
	{ key: "cpu", label: "CPU", mono: true },
	{ key: "mem", label: "Mem", mono: true },
	{ key: "origin", label: "Image", mono: true },
];

// The ListView instance — for the cross-link (reading its filtered rows +
// pagination, clearing a filter that hides the target VM).
const table = ref(null);

// ── Filtering ──────────────────────────────────────────────────────────────
// The whole filter — search, facets, and the count line — is declared here and
// OWNED by ListView (the shared engine does the matching). The head-count line
// reads "N of M running" until filtered, then "N of M"; it's suppressed once the
// pager owns the windowed total so the count isn't shown twice.
const filter = {
	search: ["uuid", "image", "disk_origin", "ipv6", "ipv4_guest", "reserved_ipv4", "state"],
	facets: [
		{ key: "failed", label: "failed", test: (v) => v.state === "Failed" },
		{
			key: "stopped",
			label: "stopped",
			test: (v) => v.state === "Stopped" || v.state === "Paused",
		},
		{
			key: "disk-hot",
			label: "disk hot",
			test: (v) => (v.disk_data_percent ?? v.data_percent ?? 0) >= 85,
		},
		{ key: "reserved", label: "reserved", test: (v) => !!v.reserved_ipv4 },
	],
	placeholder: "type to match uuid, image, ip…",
	// No item-count in the header at all — the pager carries the windowed total, and
	// an "N of M" line reads as redundant clutter beside the active facet chips.
	countLabel: () => "",
};

// ── Open one VM in the dock ──────────────────────────────────────────────────
const openUuid = ref(null);
const openVmObj = computed(() => props.vms.find((v) => v.uuid === openUuid.value) || null);
function toggle(uuid) {
	openUuid.value = openUuid.value === uuid ? null : uuid;
}

// Honour a cross-link request: clear any filter that hides the VM, page to it,
// open it in the dock. Reads ListView's filtered rows + drives its pagination.
watch(
	() => props.openVm,
	(uuid) => {
		if (!uuid) return;
		if (!props.vms.some((v) => v.uuid === uuid)) return;
		nextTick(() => {
			if (!table.value) return;
			if (!table.value.filteredRows.some((v) => v.uuid === uuid)) table.value.clearFilter();
			nextTick(() => {
				const rows = table.value.filteredRows;
				const i = rows.findIndex((v) => v.uuid === uuid);
				if (i >= 0) table.value.setPage(Math.floor(i / table.value.perPage) + 1);
				openUuid.value = uuid;
			});
		});
	},
	{ immediate: true }
);

// ── Cell helpers ─────────────────────────────────────────────────────────────
const prov = (vm) => perVmProvisioning(vm);
const origin = (vm) => diskOrigin(vm);

function stateWord(vm) {
	return (vm.state || "unknown").toLowerCase();
}
</script>
