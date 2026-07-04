<template>
	<!-- The running-VM size distribution as a horizontal bar list: each size RANGE
	     gets a row — label · proportional fill · count. The fill length is the SUM
	     of the chosen resource in that range (so the heaviest range by load reads
	     longest, not the most populous); the trailing number is the VM count. A
	     minimal CPU/RAM/Disk switch re-bins by resource. This fills the panel width
	     and scales to any number of ranges. Flat mono, no colour; the fullest range
	     reads darkest. -->
	<div class="min-w-0">
		<div class="mb-3 flex items-baseline gap-4">
			<!-- The switch: three plain mono words, the active one inks up. No chrome. -->
			<div class="flex gap-3.5">
				<button
					v-for="r in resources"
					:key="r.key"
					class="bg-transparent border-0 p-0 font-mono text-xs uppercase tracking-wider cursor-pointer hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-gray-9 focus-visible:rounded-sm"
					:class="active === r.key ? 'text-ink-gray-9' : 'text-ink-gray-5'"
					@click="active = r.key"
				>
					{{ r.label }}
				</button>
			</div>
			<span class="ml-auto font-mono tabular-nums text-xs text-ink-gray-5">{{
				totalLabel
			}}</span>
		</div>

		<div v-if="dist.buckets.length" class="flex flex-col gap-2">
			<div
				v-for="b in dist.buckets"
				:key="b.lo"
				class="grid grid-cols-[7ch_1fr_5ch] items-center gap-3.5"
			>
				<span
					class="font-mono tabular-nums text-sm text-ink-gray-6 text-right whitespace-nowrap"
					>{{ b.label }}</span
				>
				<Meter :segments="[{ frac: w(b.weight) / 100, weight: 5 }]" />
				<span class="font-mono tabular-nums text-sm text-ink-gray-6 text-right">{{
					b.count
				}}</span>
			</div>
		</div>
		<p v-else class="m-0 text-sm text-ink-gray-6">No running machines to size.</p>
	</div>
</template>

<script setup>
import { ref, computed } from "vue";
import Meter from "./Meter.vue";
import { sizeDistribution } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});

const resources = [
	{ key: "cpu", label: "CPU" },
	{ key: "mem", label: "RAM" },
	{ key: "disk", label: "Disk" },
];
const active = ref("mem");

const dist = computed(() => sizeDistribution(props.state, active.value));

const totalLabel = computed(() => {
	const t = dist.value.buckets.reduce((n, b) => n + b.weight, 0);
	const rounded = t >= 100 ? Math.round(t) : Math.round(t * 10) / 10;
	return `${dist.value.total} machines · ${rounded.toLocaleString()} ${dist.value.unit}`;
});

// Fill length as a % of the fullest range's weight — so the heaviest range fills
// the bar and the rest read against it.
function w(weight) {
	const max = dist.value.max || 1;
	return weight ? Math.max(1.5, (weight / max) * 100) : 0;
}
</script>
