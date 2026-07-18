<template>
	<!-- A quiet, monochrome pager. Shown only when the list spans more than one
	     page, so short lists read as before. "N–M of T" states the window
	     honestly; prev/next step by a page. No page-number strip — one host's
	     lists are tens to low-thousands of rows, and a "showing N of M" line is
	     the required honesty when the view is windowed.

	     It sits directly under the reserved table block on every list — the same
	     fixed 10-row height everywhere lands it at the same spot. No top rule:
	     whitespace separates it from the last row, so it reads as the table's quiet
	     tail, not a bordered band. -->
	<nav
		v-if="pages > 1"
		class="flex items-baseline justify-between gap-4 pt-3 flex-none"
		aria-label="Pagination"
	>
		<span class="text-xs text-ink-gray-5 tabular-nums font-mono"
			>{{ from }}–{{ to }} of {{ total }}</span
		>
		<span class="inline-flex items-baseline gap-3.5">
			<button
				class="bg-transparent border-0 p-0 text-xs font-mono text-ink-gray-6 cursor-pointer disabled:text-ink-gray-3 disabled:cursor-default focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
				:disabled="page <= 1"
				@click="$emit('page', page - 1)"
			>
				prev
			</button>
			<span class="text-xs text-ink-gray-6 tabular-nums font-mono"
				>{{ page }} / {{ pages }}</span
			>
			<button
				class="bg-transparent border-0 p-0 text-xs font-mono text-ink-gray-6 cursor-pointer disabled:text-ink-gray-3 disabled:cursor-default focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
				:disabled="page >= pages"
				@click="$emit('page', page + 1)"
			>
				next
			</button>
		</span>
	</nav>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
	total: { type: Number, required: true },
	page: { type: Number, required: true },
	perPage: { type: Number, required: true },
});
defineEmits(["page"]);

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)));
const from = computed(() => (props.total ? (props.page - 1) * props.perPage + 1 : 0));
const to = computed(() => Math.min(props.total, props.page * props.perPage));
</script>
