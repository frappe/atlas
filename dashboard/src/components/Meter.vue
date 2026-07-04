<template>
	<!-- The one hairline fill bar. Every gauge in the dashboard is this: a 3px
	     track (bg-outline-gray-1) with one or more absolutely-positioned segments
	     over it, severity carried by CONTRAST not hue. Four callers converged here:

	       • Histogram / Storage stack — one plain fill, `segments=[{frac}]`.
	       • QuotaBar / Provisioning   — a used+idle pair plus an optional tick
	         (the physical / commit line), and a crit fill that thickens above the
	         track to read as pressure.

	     Segments are drawn left→right; each declares its own `frac` (0..1 of the
	     track) and an ink `weight` (9|8|7|6|5). The first segment may `emphasize`,
	     which on a crit weight thickens it to 4px and lifts it half a pixel so it
	     stands proud of the track — the "over budget" tell the two pressure bars
	     both hand-rolled. A `tick` (0..1) drops a 9px vertical hair (the physical
	     capacity line) when the value over-provisions past it. -->
	<div
		class="relative h-[3px] bg-outline-gray-1"
		:class="tick != null || emphasize ? 'overflow-visible' : 'overflow-hidden'"
		:role="ariaLabel ? 'img' : null"
		:aria-label="ariaLabel"
	>
		<div
			v-for="(seg, i) in laidOut"
			:key="i"
			class="absolute top-0"
			:class="ink(seg.weight) + (seg.thick ? ' h-1 -top-[0.5px]' : ' h-[3px]')"
			:style="{ left: seg.left + '%', width: seg.width + '%' }"
		/>
		<div
			v-if="tick != null"
			class="absolute -top-[3px] w-px h-[9px] bg-ink-gray-6"
			:style="{ left: clampPct(tick * 100) + '%' }"
		/>
	</div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
	// [{ frac: 0..1, weight?: 9|8|7|6|5 }] drawn left→right. Widths clamp so the
	// segments never sum past the track (the vCPU-772% overflow the pressure bars
	// guarded against) — a later segment loses width before it runs off the end.
	segments: { type: Array, required: true },
	// The first segment thickens + lifts when its weight is a crit weight (9 or 8),
	// reading as pressure. Opt-in so plain bars (Histogram) stay flat.
	emphasize: { type: Boolean, default: false },
	// Optional physical/commit line as a fraction of the track (0..1). Null hides.
	tick: { type: Number, default: null },
	ariaLabel: { type: String, default: null },
});

// Resolve each segment to a clamped left/width so nothing overflows the track,
// and decide which one thickens (first segment, crit weight, emphasize on).
const laidOut = computed(() => {
	let used = 0;
	return props.segments.map((seg, i) => {
		const width = clampPct((seg.frac ?? 0) * 100, 0, 100 - used);
		const left = used;
		used += width;
		const thick = i === 0 && props.emphasize && (seg.weight === 9 || seg.weight === 8);
		return { left, width, weight: seg.weight ?? 6, thick };
	});
});

// The ink ramp: a segment names a contrast weight, we map it to the surface fill.
// This is the mapping the four callers each copy-pasted (with drift) — now one
// table. Higher weight = darker = more pressure.
function ink(weight) {
	return (
		{
			9: "bg-ink-gray-9",
			8: "bg-ink-gray-8",
			7: "bg-ink-gray-7",
			6: "bg-ink-gray-6",
			5: "bg-ink-gray-5",
		}[weight] || "bg-ink-gray-6"
	);
}

function clampPct(n, lo = 0, hi = 100) {
	return Math.max(lo, Math.min(hi, n));
}
</script>
