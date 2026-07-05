<template>
	<!-- The stateful alert list. Rendered through the shared ListView like every
	     other section so it reads identically: a real column-header row, one line
	     per alert, uniform row height. Columns: Severity · Alert · Detail (grows) ·
	     When, plus the → VM back-link that jumps into Machines. The cleared section
	     rides the #after slot, present in the model but empty until history exists. -->
	<ListView
		:columns="cols"
		:rows="firing"
		:paginate="paginate"
		:backlink="(a) => a.vm || null"
		:row-px="34"
		:reserve="340"
		:empty-text="clearLine"
		@open-vm="$emit('open-vm', $event)"
	>
		<!-- Cleared alerts — present in the model, empty until history exists. -->
		<template #after>
			<template v-if="cleared.length">
				<div class="mt-5 mb-2 text-xs uppercase tracking-wider text-ink-gray-5">
					Recently cleared
				</div>
				<div
					v-for="a in cleared"
					:key="a.key"
					class="grid grid-cols-[1fr_max-content] gap-x-3.5 items-baseline py-2"
				>
					<span class="text-sm text-ink-gray-5">{{ a.title }} — {{ a.detail }}</span>
				</div>
			</template>
		</template>
	</ListView>
</template>

<script setup>
import { computed } from "vue";
import ListView from "./ListView.vue";
import { shortTime } from "../derive.js";

const props = defineProps({
	// { firing: [...], cleared: [...] } from derive.js alerts().
	model: { type: Object, default: () => ({ firing: [], cleared: [] }) },
	// A plain line when nothing's firing — the caller can pass the "N of M
	// nominal" summary. Shown through ListView's empty state.
	clearLine: { type: String, default: "Nothing wants a look. All nominal." },
	// The modal shows a short unpaginated list; the full Alerts page paginates so
	// a 492-alert host never scrolls the panel.
	paginate: { type: Boolean, default: false },
});
defineEmits(["open-vm"]);

const firing = computed(() => props.model?.firing || []);
const cleared = computed(() => props.model?.cleared || []);

// One line per alert, same columns/height as every other list. Severity is a
// quiet mono lead; the sentence (detail) grows to fill the row; When packs right.
const cols = [
	{ key: "severity", label: "Severity", mono: true, status: "crit" },
	{ key: "title", label: "Alert" },
	{ key: "detail", label: "Detail", grow: true, class: "text-ink-gray-6" },
	{
		key: "since",
		label: "When",
		mono: true,
		align: "right",
		format: (v) => (v ? shortTime(v, { utc: true }) : ""),
	},
];
</script>
