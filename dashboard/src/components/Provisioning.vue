<template>
	<!-- 14/11 — the fleet provisioning view. One row per resource (CPU / Memory /
	     Disk). Two facts, side by side:

	       · the BAR reads usage against PHYSICAL (the honest headroom) — solid
	         used fill on a track whose full width IS the real capacity. Never runs
	         off: it's usage-vs-total, which is ≤ 100%.
	       · the OVERCOMMIT reads as a number (×N) — committed / physical. This is
	         the "we promised more than we have" fact, kept as a value not a runaway
	         bar. A hairline COMMIT tick sits on the track only when committed ≤
	         physical (dedicated-heavy hosts); past that the ×N carries it.

	     Below each: committed / physical · ×overcommit. That's the headline; the
	     shared/dedicated split is detail and lives in the resource's own section. -->
	<div class="grid gap-[clamp(14px,2.4vh,22px)]">
		<div v-for="r in resources" :key="r.label" class="min-w-0">
			<div class="flex items-baseline justify-between mb-1.5">
				<span class="text-xs text-ink-gray-6">{{ r.label }}</span>
				<span class="text-xs text-ink-gray-6 tabular-nums font-mono">
					<span class="text-ink-gray-8">{{ r.text.used }}</span>
					<span class="text-ink-gray-5">/</span>
					<span class="text-ink-gray-5">{{ r.text.physical ?? "—" }}</span>
					<span class="text-ink-gray-5">{{ r.unit === "vCPU" ? " vCPU" : "" }}</span>
				</span>
			</div>

			<!-- Usage bar. Track = physical. Fill = used. Commit tick only when the
			     commitment fits inside physical (else the ×N below carries it). -->
			<Meter
				:segments="[{ frac: usedPct(r) / 100, weight: 6 }]"
				:tick="commitTick(r) == null ? null : commitTick(r) / 100"
				:aria-label="aria(r)"
			/>

			<!-- Underline: the overcommit factor, alone. The raw committed value and
			     the word "committed" are dropped — the ×N IS the fact ("we promised
			     N× physical"). When the host emits no provisioning data (committed 0)
			     the line stays empty rather than printing a misleading ×0.00. -->
			<div
				class="flex items-baseline justify-between gap-3 mt-1.5 text-xs text-ink-gray-5 tabular-nums font-mono"
			>
				<span class="text-ink-gray-5">
					<template v-if="r.committed > 0 && r.overcommit != null"
						>{{ fmtx(r.overcommit) }}×</template
					>
				</span>
			</div>
		</div>
	</div>
</template>

<script setup>
import { computed } from "vue";
import Meter from "./Meter.vue";
import { provisioning } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});

const model = computed(() => provisioning(props.state));
const resources = computed(() => model.value.resources);

// Used as % of physical, clamped so the bar can't run off (it's usage-vs-total).
function usedPct(r) {
	if (r.usedFrac == null) return 0;
	return clamp(r.usedFrac * 100);
}
// The commit marker sits on the track only when the commitment is INSIDE physical
// (overcommit ≤ 1 — a dedicated-heavy host). Past 1 it would leave the track, so
// we drop it and let the ×N number carry the overcommit instead.
function commitTick(r) {
	if (r.committedFrac == null || r.committedFrac > 1) return null;
	return clamp(r.committedFrac * 100);
}
// One decimal for a real overcommit (×7.3); two below ×2 so a near-capacity host
// (×0.98) doesn't round up to a misleading "×1".
function fmtx(n) {
	const dp = n < 2 ? 2 : 1;
	return n.toFixed(dp);
}
function aria(r) {
	return `${r.label}: used ${r.text.used} of ${r.text.physical ?? "unknown"}, committed ${
		r.text.committed
	}`;
}
function clamp(n, lo = 0, hi = 100) {
	return Math.max(lo, Math.min(hi, n));
}
</script>
