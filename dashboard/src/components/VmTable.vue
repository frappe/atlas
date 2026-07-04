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
		:row-key-fn="(vm) => vm.uuid"
		:open-key="openUuid"
		:dim-fn="(vm) => vm.state !== 'Running'"
		clickable
		empty-text="No machines match this filter."
		@row-click="(vm) => toggle(vm.uuid)"
	>
		<!-- Custom cell rendering — the Machines table's cells are computed
		     (state word, prov tag, used/total splits, ingress, tenant), not plain
		     values, so it renders them itself through the shared column grid. The
		     `dim` flag (a stopped/paused VM) is passed in so the computed cells read
		     a step lighter, consistently with the shared row dimming. -->
		<template #cell="{ row: vm, col, dim }">
			<template v-if="col.key === 'uuid'">{{ uuid8(vm.uuid) }}</template>

			<span v-else-if="col.key === 'state'">{{ stateWord(vm) }}</span>

			<span v-else-if="col.key === 'prov'">
				<span
					v-if="prov(vm).kind"
					class="text-2xs"
					:class="
						dim
							? 'text-ink-gray-5'
							: prov(vm).kind === 'dedicated'
							? 'text-ink-gray-8'
							: 'text-ink-gray-5'
					"
					>{{ prov(vm).kind }}</span
				>
			</span>

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
				>{{ origin(vm) }}</span
			>

			<template v-else-if="col.key === 'disk'">{{ dataPct(vm) }}</template>

			<span
				v-else-if="col.key === 'ingress'"
				:class="{
					'!text-ink-gray-9': isReserved(vm) && !dim,
					'!text-ink-gray-3': !ingress(vm),
				}"
			>
				<span v-if="ingress(vm)" :title="ingressTitle(vm)">{{ ingress(vm).label }}</span>
			</span>

			<span v-else-if="col.key === 'tenant'">
				<span
					class="text-2xs whitespace-nowrap"
					:class="
						dim
							? 'text-ink-gray-5'
							: isOperator(vm)
							? 'text-ink-gray-8'
							: 'text-ink-gray-6'
					"
					>{{ tenantLabel(vm) }}</span
				>
			</span>
		</template>

		<!-- Detail overlay: the selected VM's joined detail. Floats ON TOP of the
		     bottom of the panel (absolute) so opening a VM never reflows the table
		     or the pager. One close control, top-right, aligned to the panel edge. -->
		<template #after>
			<transition
				enter-active-class="transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none"
				leave-active-class="transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none"
				enter-from-class="opacity-0 translate-y-1.5"
				leave-to-class="opacity-0 translate-y-1.5"
			>
				<div
					v-if="openVmObj"
					class="absolute inset-x-0 bottom-0 z-[12] bg-surface-base flex flex-col min-h-0"
				>
					<div class="flex items-center gap-2.5 pt-3.5 pb-2.5 flex-none">
						<span class="font-mono tabular-nums text-ink-gray-9 text-xs">{{
							uuid8(openVmObj.uuid)
						}}</span>
						<span
							class="text-2xs whitespace-nowrap"
							:class="isOperator(openVmObj) ? 'text-ink-gray-8' : 'text-ink-gray-6'"
							>{{ tenantLabel(openVmObj) }}</span
						>
						<button
							class="ml-auto inline-flex items-center justify-center w-5 h-5 bg-transparent border-0 text-sm leading-none text-ink-gray-5 cursor-pointer p-0 rounded-sm hover:text-ink-gray-9 hover:bg-surface-gray-2 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
							aria-label="Close detail"
							@click="openUuid = null"
						>
							✕
						</button>
					</div>
					<div class="pb-4 min-h-0">
						<VmDetail
							:state="state"
							:vm="openVmObj"
							:uplink="uplink"
							@open-image="$emit('open-image', openVmObj)"
						/>
					</div>
				</div>
			</transition>
		</template>
	</ListView>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";
import VmDetail from "./VmDetail.vue";
import ListView from "./ListView.vue";
import UsedTotal from "./UsedTotal.vue";
import {
	diskOrigin,
	vmIngress,
	vmTenant,
	isOperator as isOp,
	uuid8,
	perVmProvisioning,
} from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
	vms: { type: Array, required: true },
	uplink: { type: String, default: "eth0" },
	// Cross-link entry: when a section back-link opens a VM, App.vue sets this.
	openVm: { type: String, default: null },
});
defineEmits(["open-image"]);

// Column defs for the shared table. Every cell is rendered via the #cell slot;
// `align`/`mono` still drive the shared cell classes. Image column grows.
const cols = [
	{ key: "uuid", label: "UUID", mono: true },
	{ key: "state", label: "State" },
	{ key: "prov", label: "Prov" },
	{ key: "cpu", label: "CPU", mono: true },
	{ key: "mem", label: "Mem", mono: true },
	{ key: "origin", label: "Image", grow: true },
	{ key: "disk", label: "Disk %", mono: true, align: "right" },
	{ key: "ingress", label: "Ingress", mono: true },
	{ key: "tenant", label: "Tenant" },
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
	search: [
		"uuid",
		"image",
		"disk_origin",
		"ipv6",
		"ipv4_guest",
		"reserved_ipv4",
		"tenant",
		"state",
		"role",
	],
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
		{ key: "operator", label: "operator", test: (v) => isOp(v) },
	],
	placeholder: "type to match uuid, image, ip, tenant…",
	countLabel: (shown, total) => {
		const filtering = shown !== total;
		const perPage = table.value?.perPage ?? 10;
		if (filtering) return shown > perPage ? "" : `${shown} of ${total}`;
		const running = props.vms.filter((v) => v.state === "Running").length;
		return `${running} of ${total} running`;
	},
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
const isOperator = (vm) => isOp(vm);
function tenantLabel(vm) {
	const t = vmTenant(vm);
	return t === "operator" ? "operator" : t;
}

const ingress = (vm) => vmIngress(props.state, vm);
const isReserved = (vm) => ingress(vm)?.kind === "reserved";
function ingressTitle(vm) {
	const i = ingress(vm);
	return i ? (i.kind === "reserved" ? "reserved public IPv4" : "proxy / TCP map") : "";
}
function dataPct(vm) {
	const v = vm.disk_data_percent ?? vm.data_percent;
	return v == null ? "" : Math.round(v) + "%";
}

function stateWord(vm) {
	return (vm.state || "unknown").toLowerCase();
}
</script>
