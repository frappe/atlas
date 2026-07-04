<template>
	<!-- The stateful alert list: firing now, and (once the backend carries
	     history) recently cleared. Each row is a plain sentence with a severity
	     word, a since-time, and — when it maps to a VM — a jump into Machines.
	     Rendered both on the Alerts page and inside the header badge modal.

	     Renders through the shared ListView like every other list: the two-line
	     body and the when/jump cell go through #cell; the cleared section rides the
	     #after slot. The column-header row and pager only appear on the full page
	     (paginate); the modal shows a bare, self-scrolling list. -->
	<ListView
		:columns="cols"
		:rows="firing"
		:paginate="paginate"
		:hide-header="true"
		cell-align="align-top"
		:row-px="42"
		:reserve="340"
		:empty-text="clearLine"
		@open-vm="$emit('open-vm', $event)"
	>
		<template #cell="{ row: a, col }">
			<div v-if="col.key === 'alert'">
				<div class="flex items-baseline gap-2 leading-5">
					<span class="text-sm text-ink-gray-8 font-medium">{{ a.title }}</span>
					<span
						class="text-2xs uppercase tracking-wider font-normal"
						:class="
							a.severity === 'crit'
								? 'text-ink-gray-9'
								: a.severity === 'warn'
								? 'text-ink-gray-7'
								: 'text-ink-gray-5'
						"
						>{{ a.severity }}</span
					>
				</div>
				<div class="text-sm text-ink-gray-6 mt-0.5 leading-normal">{{ a.detail }}</div>
			</div>
			<div
				v-else
				class="flex items-center justify-end gap-3.5 whitespace-nowrap leading-5 h-5"
			>
				<span v-if="a.since" class="text-2xs text-ink-gray-5 font-mono tabular-nums">{{
					short(a.since)
				}}</span>
				<button
					v-if="a.vm"
					class="bg-transparent border-0 p-0 font-mono tabular-nums text-xs text-ink-gray-7 cursor-pointer no-underline hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
					@click="$emit('open-vm', a.vm)"
				>
					{{ uuid8(a.vm) }}
				</button>
			</div>
		</template>

		<!-- Cleared alerts — present in the model, empty until history exists. We
		     show the section header only when there's something in it, so the page
		     stays honest about not fabricating a history. -->
		<template #after>
			<template v-if="cleared.length">
				<div class="mt-5 mb-2 text-2xs uppercase tracking-wider text-ink-gray-5">
					Recently cleared
				</div>
				<div
					v-for="a in cleared"
					:key="a.key"
					class="grid grid-cols-[1fr_max-content] gap-x-3.5 items-start py-2.5"
				>
					<div>
						<div class="flex items-baseline gap-2 leading-5">
							<span class="text-sm text-ink-gray-5 font-medium">{{ a.title }}</span>
						</div>
						<div class="text-sm text-ink-gray-5 mt-0.5 leading-normal">
							{{ a.detail }}
						</div>
					</div>
				</div>
			</template>
		</template>
	</ListView>
</template>

<script setup>
import { computed } from "vue";
import ListView from "./ListView.vue";
import { shortTime, uuid8 } from "../derive.js";

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

// The alert body grows (two-line sentence); the when/jump cell packs right.
const cols = [
	{ key: "alert", label: "Alert", grow: true, wrap: true },
	{ key: "when", label: "When", align: "right" },
];

const short = (iso) => shortTime(iso, { utc: true });
</script>
