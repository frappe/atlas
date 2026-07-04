<template>
	<!-- The signature element: a VM's reachability as its ACTUAL directed shape,
	     not a flat rule list. Each leg is [dir | flow]. The flow is SF-Mono nodes
	     joined by →; the "key" node (the public reserved IP) is the darkest ink;
	     the transform (DNAT/SNAT/masquerade/routed /128) is a small hairline tag. -->
	<div class="grid gap-2">
		<div
			v-for="(leg, i) in legs"
			:key="i"
			class="grid grid-cols-[68px_1fr] items-baseline gap-3.5"
		>
			<span class="text-xs font-medium text-ink-gray-7 whitespace-nowrap">{{
				leg.dir
			}}</span>
			<span class="flex flex-wrap items-baseline gap-2 min-w-0">
				<span
					class="font-mono tabular-nums text-sm break-all"
					:class="leg.from === keyOf(leg) ? 'text-ink-gray-9' : 'text-ink-gray-8'"
					>{{ leg.from }}</span
				>
				<span class="text-ink-gray-3 text-xs" aria-hidden="true">→</span>
				<template v-if="leg.hop">
					<span class="font-mono tabular-nums text-sm break-all text-ink-gray-5">{{
						leg.hop
					}}</span>
					<span class="text-ink-gray-3 text-xs" aria-hidden="true">→</span>
				</template>
				<span
					class="font-mono tabular-nums text-sm break-all"
					:class="leg.to === keyOf(leg) ? 'text-ink-gray-9' : 'text-ink-gray-8'"
					>{{ leg.to }}</span
				>
				<span
					class="font-sans text-2xs tracking-tight text-ink-gray-6 whitespace-nowrap"
					>{{ leg.xf }}</span
				>
			</span>
		</div>
	</div>
</template>

<script setup>
defineProps({
	// [{ dir, from, hop?, to, xf, key? }]
	legs: { type: Array, required: true },
});

// The key (darkest) node is the public reserved IP. A leg marks it with key:true;
// it's whichever endpoint is the reserved address — `from` on In v4, `to` on Out v4.
function keyOf(leg) {
	if (!leg.key) return null;
	// The reserved IP is the endpoint that is NOT the guest / an interface name.
	// In v4: from is reserved. Out v4: to is reserved.
	return leg.dir === "Out v4" ? leg.to : leg.from;
}
</script>
