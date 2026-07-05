<template>
	<!-- Analytics: the deep view. When the host carries a metrics series, this
	     shows the time-series as flat area charts (CPU, memory, disk IO, network,
	     pool). Without one it stays HONEST — the range tabs remain but each panel
	     states there's no history and shows the current-snapshot shape it CAN
	     prove (the size distribution, current counts). No fabricated trend lines. -->
	<div class="min-w-0">
		<!-- Real series → charts. -->
		<template v-if="series.length">
			<MetricGrid :series="series" :cols="2" class="mb-10" />
			<section>
				<PanelHead title="Fleet" h3 />
				<Histogram :state="state" />
			</section>
		</template>

		<!-- No series → the empty chart skeleton (honest "no history" frame, design
		     intact) + note + what the snapshot proves. -->
		<template v-else>
			<p class="m-0 mb-8 text-sm text-ink-gray-6 max-w-[60ch] leading-relaxed">
				No metrics history on this host yet — this dashboard reads a single live snapshot.
				Time-series will populate once the metrics series ships. Below is what the current
				snapshot proves.
			</p>

			<MetricGrid :series="skeleton" :cols="2" class="mb-10" />

			<section class="mb-9">
				<PanelHead title="Fleet" h3 />
				<Histogram :state="state" />
			</section>

			<section class="mb-9">
				<PanelHead title="Current state" h3 />
				<dl
					class="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-x-6 gap-y-2.5 m-0"
				>
					<template v-for="c in stateCounts" :key="c.k">
						<dt class="text-xs text-ink-gray-6">{{ c.k }}</dt>
						<dd class="mt-0.5 text-2xl text-ink-gray-8 tabular-nums">{{ c.v }}</dd>
					</template>
				</dl>
			</section>
		</template>
	</div>
</template>

<script setup>
import { computed } from "vue";
import Histogram from "./Histogram.vue";
import MetricGrid from "./MetricGrid.vue";
import PanelHead from "./PanelHead.vue";
import { metrics, metricSkeleton } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});

const series = computed(() => metrics(props.state));
const skeleton = metricSkeleton();

const stateCounts = computed(() => {
	const vms = props.state.virtual_machines || [];
	const by = {};
	for (const v of vms) by[v.state] = (by[v.state] || 0) + 1;
	const rows = Object.entries(by).map(([k, v]) => ({ k, v }));
	rows.push({ k: "Migrating", v: vms.filter((v) => v.migrating).length });
	rows.push({ k: "Reserved v4", v: vms.filter((v) => v.reserved_ipv4).length });
	return rows;
});
</script>
