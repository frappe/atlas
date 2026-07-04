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

	     Below each: committed / physical, and the shared·dedicated split of the
	     commitment (shared = the reclaimable overprovision, dedicated = exact). -->
	<div class="grid gap-[clamp(14px,2.4vh,22px)]">
		<div v-for="r in resources" :key="r.label" class="min-w-0">
			<div class="flex items-baseline justify-between mb-1.5">
				<span class="text-xs text-ink-gray-6">{{ r.label }}</span>
				<span class="text-xs text-ink-gray-6 tabular-nums font-mono">
					<span
						:class="
							r.severity === 'crit'
								? 'text-ink-gray-9 font-medium'
								: r.severity === 'warn'
								? 'text-ink-gray-9'
								: 'text-ink-gray-8'
						"
						>{{ r.text.used }}</span
					>
					<span class="text-ink-gray-5">/</span>
					<span class="text-ink-gray-5">{{ r.text.physical ?? "—" }}</span>
					<span class="text-ink-gray-5">{{ r.unit === "vCPU" ? " vCPU" : "" }}</span>
				</span>
			</div>

			<!-- Usage bar. Track = physical. Fill = used. Commit tick only when the
			     commitment fits inside physical (else the ×N below carries it). -->
			<Meter
				:segments="[{ frac: usedPct(r) / 100, weight: sevWeight(r.severity) }]"
				emphasize
				:tick="commitTick(r) == null ? null : commitTick(r) / 100"
				:aria-label="aria(r)"
			/>

			<!-- Underline: overcommit factor (the headline fact) + the split. When
			     the host emits no provisioning data (committed 0), we suppress the
			     ×N and the split rather than print a misleading ×0.00 / all-zero
			     line — honest silence over invented precision. -->
			<div
				class="flex items-baseline justify-between gap-3 mt-1.5 text-2xs text-ink-gray-5 tabular-nums font-mono"
			>
				<span :class="r.overcommit > 1 ? 'text-ink-gray-8' : 'text-ink-gray-6'">
					<template v-if="r.committed > 0 && r.overcommit != null">
						committed {{ r.text.committed }} · ×{{ fmtx(r.overcommit) }}
					</template>
					<template v-else-if="r.committed > 0"
						>committed {{ r.text.committed }}</template
					>
				</span>
				<span v-if="hasSplit(r)" class="text-ink-gray-5 text-right">
					shared {{ r.text.shared }} · dedicated {{ r.text.dedicated }}
				</span>
			</div>
		</div>

		<p class="mt-0.5 text-2xs text-ink-gray-5 tabular-nums font-mono">
			<template v-if="counts.shared || counts.dedicated">
				{{ counts.shared }} shared · {{ counts.dedicated }} dedicated ·
				{{ counts.running }} running
			</template>
			<template v-else>{{ counts.running }} running</template>
		</p>
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
const counts = computed(() => model.value.counts);

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
// The shared/dedicated split only reads when the host actually tagged VMs — a
// sparse real host with no provisioning fields shows an honest blank instead.
function hasSplit(r) {
	return r.sharedCommitted > 0 || r.dedicatedCommitted > 0;
}
function aria(r) {
	return `${r.label}: used ${r.text.used} of ${r.text.physical ?? "unknown"}, committed ${
		r.text.committed
	}`;
}
function clamp(n, lo = 0, hi = 100) {
	return Math.max(lo, Math.min(hi, n));
}
// Usage severity → Meter ink weight (contrast, not colour). crit also thickens
// the fill (Meter's `emphasize`) to read as pressure.
function sevWeight(severity) {
	return { crit: 9, warn: 8, ok: 6 }[severity] ?? 6;
}
</script>
