<template>
	<!-- Storage → Analytics: the LVM stack (item 16-A). A compact PV → VG → pool
	     summary shows the whole tree in one glance — physical volume, volume group,
	     thin pool — each as used/total in ONE unit plus a hairline fill bar. This is
	     the non-table half of Storage; the Volumes list is a plain ListView section
	     (Storage → Volumes). All sizes read from the `_bytes` fields so units stay
	     consistent (G, T above 1024G). -->
	<div class="min-w-0">
		<PanelHead title="Analytics" h3 />
		<!-- Stack summary: three rows, physical → group → pool, each a labelled
		     used/total with a thin fill bar. Reads top-to-bottom as the allocation
		     chain. -->
		<div class="grid gap-3 mt-1 flex-none">
			<!-- Three parts across: the label (fixed), the fill bar (grows), and a
			     right-stacked read+meta (fixed). A flex row rather than a
			     grid-template-areas grid, so no scoped CSS is needed. -->
			<div v-for="s in stack" :key="s.k" class="flex items-center gap-x-5">
				<div class="flex flex-col gap-px w-[150px] flex-none">
					<span class="text-xs text-ink-gray-8">{{ s.k }}</span>
					<span class="text-xs text-ink-gray-5 font-mono tabular-nums">{{
						s.name
					}}</span>
				</div>
				<Meter class="flex-1" :segments="[{ frac: s.frac, weight: 6 }]" />
				<div class="w-[190px] flex-none flex flex-col gap-y-0.5">
					<UsedTotal
						class="text-xs text-right font-mono tabular-nums"
						:used="s.used != null ? fmtGiB(s.used) : null"
						:total="fmtGiB(s.total)"
						:pct="s.frac != null ? pct(s.frac) : null"
					/>
					<div class="text-xs text-ink-gray-5 text-right font-mono tabular-nums">
						{{ s.meta }}
					</div>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
import { computed } from "vue";
import PanelHead from "./PanelHead.vue";
import Meter from "./Meter.vue";
import UsedTotal from "./UsedTotal.vue";
import { storage, fmtGiB } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});

const model = computed(() => storage(props.state));

// The stack rows, physical → group → pool. Every bar reads at ONE contrast
// weight (the Meter weight is fixed in the template) — the pool no longer darkens
// as it fills, so the three bars read as one uniform stack. Missing members drop
// out.
const stack = computed(() => {
	const m = model.value;
	const rows = [];
	if (m.pv)
		rows.push({
			k: "Physical",
			name: m.pv.name,
			used: m.pv.used,
			total: m.pv.total,
			frac: m.pv.frac,
			meta: "",
		});
	if (m.vg)
		rows.push({
			k: "Group",
			name: m.vg.name,
			used: m.vg.used,
			total: m.vg.total,
			frac: m.vg.frac,
			meta: `${m.vg.lvCount} LV · ${m.vg.pvCount} PV`,
		});
	if (m.pool)
		rows.push({
			k: "Thin pool",
			name: m.pool.name,
			used: m.pool.total != null && m.pool.frac != null ? m.pool.total * m.pool.frac : null,
			total: m.pool.total,
			frac: m.pool.frac,
			meta: m.pool.metaPercent != null ? `meta ${m.pool.metaPercent}%` : "",
		});
	return rows;
});

// A fraction (0–1) → whole percent for the used/total read.
function pct(frac) {
	return frac == null ? 0 : Math.round(frac * 100);
}
</script>
