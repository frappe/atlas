<template>
	<!-- A VM's reachability as a plain per-leg definition list: the direction is
	     the label (aligned to the Machine grid's 82px column above it), the flow is
	     one mono value — endpoints joined by a quiet → so direction still reads,
	     with the transform (DNAT / masquerade / routed /128) as a trailing note.
	     Same facts as the old directed diagram, far fewer elements: no per-node
	     key-ink, no hairline tag, no flex scaffolding. -->
	<dl class="grid gap-2 m-0">
		<div
			v-for="(leg, i) in legs"
			:key="i"
			class="grid grid-cols-[82px_1fr] items-baseline gap-3.5"
		>
			<dt class="text-xs text-ink-gray-6 whitespace-nowrap">{{ leg.dir }}</dt>
			<dd class="m-0 font-mono tabular-nums text-sm text-ink-gray-8 break-all">
				{{ flow(leg) }}<span v-if="leg.xf" class="text-ink-gray-5"> · {{ leg.xf }}</span>
			</dd>
		</div>
	</dl>
</template>

<script setup>
defineProps({
	// [{ dir, from, hop?, to, xf }]
	legs: { type: Array, required: true },
});

// The flow as one string: from → (hop →) to. A plain arrow keeps the direction
// legible without the per-node ink emphasis the diagram carried.
function flow(leg) {
	return [leg.from, leg.hop, leg.to].filter(Boolean).join(" → ");
}
</script>
