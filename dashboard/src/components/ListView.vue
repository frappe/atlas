<template>
	<!-- The one monochrome list. EVERY list view in the dashboard renders through
	     this: object sections (Images, Network, System…), the Firewall ruleset, the
	     Storage volumes, the Events log, the Alerts list, and — via the #cell slot +
	     clickable rows — the Machines table. Column defs are passed in; the header,
	     the filter toolbar, pagination, the empty state, back-links, dimmed rows,
	     and the fill layout all live here once so no view re-implements them.

	     One value per cell is the baseline: the column carries the value and, when it
	     needs shaping (a uuid → 8 chars, a data_percent → "N%", a unit's "active ·
	     running"), a `format(value, row)` fn on the column def — the ONE place cell
	     formatting happens, so no two lists shape the same field differently.
	     Consumers that need more than a value (Machines' used/total splits, Alerts'
	     two-line body) provide a #cell scoped slot and render the cell body
	     themselves while still inheriting the whole table + pager machinery. A
	     bespoke header goes through #head; a searchable list passes a `filter`
	     config (search fields + facet defs) and ListView owns the query, the facet
	     state, and the filtering — the toolbar comes for free. -->
	<div class="mb-10 last:mb-0 flex flex-col min-h-0 flex-1">
		<!-- Optional header. Most object tables pass a title (rendered through the
		     shared PanelHead); a searchable list also gets the shared filter toolbar
		     on the right; a fully bespoke header goes through #head. -->
		<slot name="head">
			<PanelHead v-if="title || filter" :title="title" :summary="summary" :h3="h3">
				<template #right>
					<button
						v-if="hasToggle"
						class="p-0 border-0 bg-transparent text-xs text-ink-gray-6 cursor-pointer whitespace-nowrap font-mono tabular-nums hover:text-ink-gray-8 hover:underline hover:decoration-dotted"
						type="button"
						@click="showVm = !showVm"
					>
						{{ showVm ? `hide ${noun}` : `+${hiddenCount} ${noun}` }}
					</button>
					<ListFilter
						v-if="filter"
						class="ml-auto"
						:facets="offeredFacets"
						:query="query"
						:active="activeFacets"
						:count-label="countLine"
						:placeholder="filter.placeholder || 'type to filter…'"
						@update:query="setQuery"
						@toggle-facet="toggleFacet"
					/>
				</template>
			</PanelHead>
		</slot>

		<!-- A pre-table strip (the Firewall structure line, the Storage stack). -->
		<slot name="strip" />

		<p v-if="!visibleRows.length" class="m-0 text-sm text-ink-gray-6">{{ emptyText }}</p>

		<template v-else>
			<div class="overflow-auto -mx-3 flex-1 min-h-0">
				<table class="w-full border-collapse text-sm">
					<thead v-if="!hideHeader">
						<tr>
							<th
								v-for="c in columns"
								:key="c.key"
								class="font-normal text-2xs tracking-wider uppercase text-ink-gray-5 pt-0 pr-6 pb-2 pl-3 whitespace-nowrap sticky top-0 bg-surface-base z-[1]"
								:class="[
									c.align === 'right' ? 'text-right pr-3' : 'text-left',
									c.grow ? 'w-full' : '',
								]"
							>
								{{ c.label }}
							</th>
							<th
								v-if="hasBacklinks"
								class="font-normal text-2xs tracking-wider uppercase text-ink-gray-5 pt-0 pb-2 whitespace-nowrap sticky top-0 bg-surface-base z-[1] text-right w-[1%]"
							>
								VM
							</th>
							<!-- A trailing spacer soaks up the leftover width of the `w-full`
							     table, so the real columns pack tight to the left and stay
							     content-sized (each as wide as its value needs — the
							     variable-width behaviour) instead of the auto layout spreading
							     the slack across them with mis-reading gaps. A `grow` column,
							     when present, absorbs the slack instead and the table fills. -->
							<th
								v-if="!hasGrow"
								class="w-full !p-0 border-0 sticky top-0 bg-surface-base z-[1]"
								aria-hidden="true"
							></th>
						</tr>
					</thead>
					<tbody>
						<tr
							v-for="(row, i) in pageRows"
							:key="rowKey(row, i)"
							class="drow group"
							:class="[
								clickable
									? 'cursor-pointer focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-[-2px]'
									: '',
								{ on: isOpen(row) },
							]"
							:tabindex="clickable ? 0 : null"
							:role="clickable ? 'button' : null"
							:aria-expanded="clickable ? isOpen(row) : null"
							@click="clickable && $emit('row-click', row)"
							@keydown.enter.prevent="clickable && $emit('row-click', row)"
							@keydown.space.prevent="clickable && $emit('row-click', row)"
						>
							<td
								v-for="c in columns"
								:key="c.key"
								class="pt-2 pr-4 pb-2 pl-3"
								:class="[
									cellAlign,
									cellFlow(c),
									c.grow ? 'w-full' : '',
									c.align === 'right' ? 'text-right' : '',
									c.mono ? 'font-mono tabular-nums' : '',
									cellInk(row, c),
									clickable
										? 'group-hover:bg-surface-gray-2 group-[.on]:bg-surface-gray-3'
										: '',
									cellEmpty(row, c) ? '!text-ink-gray-3' : '',
								]"
							>
								<!-- A #cell scoped slot lets a consumer render bespoke cell
								     content (multi-span used/total, two-line alert body) while
								     keeping this component's columns/pagination. Falls back to the
								     built-in value rendering (format fn + status ink). -->
								<slot name="cell" :row="row" :col="c" :dim="dim(row)">
									<span
										:class="[
											c.class,
											c.status
												? statusOn(row, c)
													? 'text-ink-gray-8'
													: 'text-ink-gray-5'
												: '',
										]"
									>
										{{ display(row[c.key], c, row) }}
									</span>
								</slot>
							</td>
							<td
								v-if="hasBacklinks"
								class="pt-2 pr-4 pb-2 pl-3 align-baseline whitespace-nowrap text-right w-[1%]"
								:class="
									clickable
										? 'group-hover:bg-surface-gray-2 group-[.on]:bg-surface-gray-3'
										: ''
								"
							>
								<VmLink :vm="ownerOf(row)" @open-vm="$emit('open-vm', $event)" />
							</td>
							<td
								v-if="!hasGrow"
								class="w-full !p-0"
								:class="
									clickable
										? 'group-hover:bg-surface-gray-2 group-[.on]:bg-surface-gray-3'
										: ''
								"
								aria-hidden="true"
							></td>
						</tr>
					</tbody>
				</table>
			</div>

			<Pager
				v-if="paginate"
				:total="visibleRows.length"
				:page="page"
				:per-page="perPage"
				@page="setPage"
			/>
		</template>

		<!-- A trailing slot for anything that floats below/over the table
		     (Machines' detail dock, the Alerts "recently cleared" section). -->
		<slot
			name="after"
			:page-rows="pageRows"
			:per-page="perPage"
			:page="page"
			:set-page="setPage"
		/>
	</div>
</template>

<script setup>
import { computed, ref, toRef, watch } from "vue";
import Pager from "./Pager.vue";
import PanelHead from "./PanelHead.vue";
import ListFilter from "./ListFilter.vue";
import VmLink from "./VmLink.vue";
import { usePagedRows } from "../usePagedRows.js";

const props = defineProps({
	// [{ key, label, align?, mono?, grow?, wrap?, status?, format?, class? }]
	//   format: (value, row) => string — shape a cell's text (uuid8, "N%"). The one
	//           place cell formatting lives; no consumer re-does it in a #cell slot.
	//   status: the value that reads as "on" (a unit's "active", an iface's "UP").
	//           On → darker ink, off → lighter. Text still comes from value/format.
	//   class:  extra classes on the cell body (a quiet mono/uppercase lead like the
	//           Events time/kind) — so a purely-styled cell needs no #cell slot.
	columns: { type: Array, required: true },
	rows: { type: Array, required: true },
	// Header (optional — omit and pass #head for a bespoke header).
	title: { type: String, default: "" },
	summary: { type: String, default: "" },
	// Lighter section heading (h3) — default for the object sections.
	h3: { type: Boolean, default: true },
	// Drop the column-header row (a bare log like Events with a #cell body).
	hideHeader: { type: Boolean, default: false },
	// The most-important column (darkest ink) — a reserved v4, an image name.
	keyCol: { type: String, default: null },
	// (row) => uuid|null — a back-link resolver. When set, a VM column renders.
	backlink: { type: Function, default: null },
	// Pagination sizing — a taller row / a pre-table strip reserves more height.
	rowPx: { type: Number, default: 34 },
	reserve: { type: Number, default: 360 },
	// Windowed pagination. Off shows the whole list (the compact Alerts modal,
	// which scrolls itself).
	paginate: { type: Boolean, default: true },
	// host-only filtering. When set, hides per-VM rows behind a header toggle:
	//   { isVm: (row)=>bool, isBroken?: (row)=>bool, noun?: "VM-related" }
	vmFilter: { type: Object, default: null },
	// Clickable rows (Machines). Emits `row-click`; `openKey`/`rowKeyFn` mark the
	// open row so the consumer's selection highlights.
	clickable: { type: Boolean, default: false },
	// (row) => stable key; defaults to index. Also identifies the open row.
	rowKeyFn: { type: Function, default: null },
	// The key of the currently-open row (compared against rowKeyFn(row)).
	openKey: { type: [String, Number], default: null },
	// (row) => bool — a dimmed row (a stopped/paused VM reads a step lighter).
	// Applied per cell as an ink class, so it works across the #cell slot too.
	dimFn: { type: Function, default: null },
	// Cell vertical alignment. Baseline reads as a table; two-line rows (Alerts)
	// pass "align-top".
	cellAlign: { type: String, default: "align-baseline" },
	// Empty-state text.
	emptyText: { type: String, default: "None." },
	// ── declarative search + facets (opt-in) ──
	// Pass a `filter` config and ListView owns the whole thing — the search string,
	// the active facets, and the filtering itself — so any section gets search with
	// one prop (no repeating VmTable's plumbing). Shape:
	//   {
	//     search: ['uuid','image', (row)=>row.foo],  // fields (or fns) to match
	//     facets: [{ key, label, test:(row)=>bool }], // offered only when count > 0
	//     placeholder?: '…',
	//     countLabel?: (shown, total) => '12 of 400',  // left-of-chips count line
	//   }
	filter: { type: Object, default: null },
});
const emit = defineEmits(["open-vm", "row-click", "filter"]);

const hasBacklinks = computed(() => props.backlink != null);
const ownerOf = (row) => (props.backlink ? props.backlink(row) : null);

// ── declarative search + facets ──────────────────────────────────────────────
// ListView owns the query + active facets and does the filtering, so a section
// opts in with a single `filter` prop. `searchRows` applies the free-text search
// (substring across the configured fields), `facetRows` the active facet tests;
// `filteredRows` is rows after both. The query/facets reset when the source
// changes (a new fixture). Emits `filter` with {query, facets, shown, total} so a
// parent that needs to react (a cross-link that must clear a hiding filter) can.
const query = ref("");
const activeFacets = ref(new Set());

// A facet is offered only when at least one row matches it — no dead chips.
const offeredFacets = computed(() => {
	if (!props.filter?.facets) return [];
	return props.filter.facets
		.map((f) => ({ ...f, count: props.rows.filter((r) => f.test(r)).length }))
		.filter((f) => f.count > 0);
});

// Pull a searchable field off a row — a key name or a (row)=>value accessor.
function fieldValue(row, field) {
	return typeof field === "function" ? field(row) : row[field];
}
function matchesQuery(row) {
	const q = query.value.trim().toLowerCase();
	if (!q) return true;
	const hay = (props.filter.search || [])
		.map((f) => fieldValue(row, f))
		.filter((v) => v != null && v !== "")
		.join(" ")
		.toLowerCase();
	return q.split(/\s+/).every((term) => hay.includes(term));
}
function matchesFacets(row) {
	for (const key of activeFacets.value) {
		const def = props.filter.facets?.find((f) => f.key === key);
		if (def && !def.test(row)) return false;
	}
	return true;
}
const isFiltering = computed(() => query.value.trim() !== "" || activeFacets.value.size > 0);
const filteredRows = computed(() => {
	if (!props.filter) return props.rows;
	return props.rows.filter((r) => matchesQuery(r) && matchesFacets(r));
});
// The left-of-chips count line, from the config's countLabel(shown, total) — or a
// plain "N of M" while filtering.
const countLine = computed(() => {
	if (!props.filter) return "";
	const total = props.rows.length;
	const shown = filteredRows.value.length;
	if (props.filter.countLabel) return props.filter.countLabel(shown, total);
	return isFiltering.value ? `${shown} of ${total}` : "";
});

function emitFilter() {
	emit("filter", {
		query: query.value,
		facets: activeFacets.value,
		shown: filteredRows.value.length,
		total: props.rows.length,
	});
}
function setQuery(q) {
	query.value = q;
	emitFilter();
}
function toggleFacet(key) {
	const s = new Set(activeFacets.value);
	s.has(key) ? s.delete(key) : s.add(key);
	activeFacets.value = s;
	emitFilter();
}
function clearFilter() {
	query.value = "";
	activeFacets.value = new Set();
}
// A fresh source (new fixture) clears the filter.
watch(
	() => props.rows,
	() => clearFilter()
);

// A `grow` column absorbs the table's leftover width (it fills), so when one is
// present the trailing spacer is dropped — the grow cell packs the slack itself.
// Without a grow column the spacer stays and every column packs tight to the
// left at its own content width.
const hasGrow = computed(() => props.columns.some((c) => c.grow));

// ── host-only filtering ──
// Runs AFTER the search/facet filter, on `filteredRows`, so the fold-behind
// toggle and its count reflect the current search.
const showVm = ref(false);
const isVm = (r) => (props.vmFilter ? props.vmFilter.isVm(r) : false);
const isBroken = (r) => (props.vmFilter?.isBroken ? props.vmFilter.isBroken(r) : false);
const noun = computed(() => props.vmFilter?.noun || "VM-related");
const hiddenCount = computed(() => {
	if (!props.vmFilter) return 0;
	return filteredRows.value.filter((r) => isVm(r) && !isBroken(r)).length;
});
const hasToggle = computed(() => props.vmFilter != null && hiddenCount.value > 0);
const visibleRows = computed(() => {
	const rows = filteredRows.value;
	if (!props.vmFilter || showVm.value) return rows;
	return rows.filter((r) => !isVm(r) || isBroken(r));
});

// rowPx/reserve are fixed per mounted table (usePageSize reads them once at
// setup), so pass the plain prop values — not computeds, which it can't unref.
const { page, pageRows, perPage, setPage } = usePagedRows(visibleRows, {
	rowPx: props.rowPx,
	reserve: props.reserve,
	enabled: toRef(props, "paginate"),
});

// ── row identity + clickable state ──
const rowKey = (row, i) => (props.rowKeyFn ? props.rowKeyFn(row) : i);
const isOpen = (row) =>
	props.clickable &&
	props.openKey != null &&
	props.rowKeyFn != null &&
	props.rowKeyFn(row) === props.openKey;
const dim = (row) => (props.dimFn ? props.dimFn(row) : false);

// How a cell handles overflow. Every cell is `nowrap` and sized to its own text,
// so a `grow` column carrying a wide value (a full IPv6 address, an image name)
// gets the room it needs on one line while short columns stay narrow. `c.wrap`
// is the rare explicit opt-in for a column that should wrap onto multiple lines
// (the Alerts two-line body).
function cellFlow(c) {
	return c.wrap ? "whitespace-normal break-words" : "whitespace-nowrap";
}

// Per-cell ink level. The key column reads darkest; dimmed rows (a stopped VM)
// read a step lighter across the whole row — replacing what used to be a
// cross-component :deep(.stopped) rule in the Machines table.
function cellInk(row, c) {
	if (dim(row)) return "text-ink-gray-5";
	return c.key === props.keyCol ? "text-ink-gray-9" : "text-ink-gray-8";
}

// Expose pagination state so a parent that needs to coordinate (Machines'
// cross-link page-jump, its head-count logic) can read/drive it via a template
// ref — without every other consumer carrying that in the prop surface.
defineExpose({
	page,
	perPage,
	setPage,
	pageRows,
	visibleRows,
	filteredRows,
	// Filter controls, so a parent cross-link can page/open a row hidden by a
	// filter: clear it, then find the row's index in filteredRows/visibleRows.
	query,
	activeFacets,
	clearFilter,
});

// A status cell is "on" when its value equals the column's declared on-value
// (a unit's "active", an iface's "UP") — on reads darker, off lighter. Ink only;
// the text comes from value/format like every other cell.
function statusOn(row, c) {
	return row[c.key] === c.status;
}

// The one cell-text path. A column's `format(value, row)` shapes the text (uuid8,
// "N%", a unit's "active · running"); otherwise booleans read yes/no and empties
// read blank. No key-name special-casing — the shaping lives on the column def,
// next to the data it describes, so no two lists format the same field differently.
function display(value, col, row) {
	if (col.format) return col.format(value, row);
	if (value === true) return "yes";
	if (value === false) return "no";
	if (value === null || value === undefined || value === "") return "";
	return value;
}
function cellEmpty(row, col) {
	const v = row[col.key];
	return v === null || v === undefined || v === "";
}
</script>
