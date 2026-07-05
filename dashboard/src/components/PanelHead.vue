<template>
	<!-- Panel heading: title · count · summary, with an optional right-side slot
	     (VmTable's search, Overview's "recent"). `h3` renders a lighter section
	     heading. One row, three ink levels — all one title size (14px/500). -->
	<div class="flex items-baseline gap-3 mb-3 flex-none">
		<component :is="h3 ? 'h3' : 'h2'" class="m-0 text-base font-medium text-ink-gray-9">{{
			title
		}}</component>
		<span v-if="count != null" class="text-xs text-ink-gray-5 font-mono tabular-nums">
			{{ count }}<slot name="count" />
		</span>
		<span v-if="summary" class="ml-auto text-xs text-ink-gray-6">{{ summary }}</span>
		<!-- The right cluster always pins to the panel's right edge. When there's no
		     summary to carry the ml-auto push, this wrapper does it. -->
		<div
			v-if="$slots.right"
			class="flex items-baseline gap-3.5"
			:class="{ 'ml-auto': !summary }"
		>
			<slot name="right" />
		</div>
	</div>
</template>

<script setup>
defineProps({
	title: { type: String, required: true },
	count: { type: [Number, String], default: null },
	summary: { type: String, default: "" },
	h3: { type: Boolean, default: false },
});
</script>
