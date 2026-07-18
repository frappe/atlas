<template>
	<!-- A flat monochrome sparkline: a 1px straight-segment trace over a single
	     mid grid line, no axes, no fill, no colour. The header carries the series
	     name and its current (last) value — the chart is read at a glance, the
	     number is the fact. Honest: renders only from real series points; one
	     point draws a dot, not a fabricated trend, and the polyline is the raw
	     sampled shape (no smoothing that invents motion the data doesn't have). -->
	<div class="min-w-0">
		<div class="flex justify-between items-baseline gap-3 mb-2">
			<span class="text-ink-gray-8 text-sm min-w-0 truncate">{{ label }}</span>
			<!-- When a formatter is given it owns the unit; otherwise append it. -->
			<span class="font-mono tabular-nums text-ink-gray-6 text-xs whitespace-nowrap"
				><b class="text-ink-gray-9 font-medium">{{ current }}</b
				><template v-if="unit && !format"> {{ unit }}</template></span
			>
		</div>
		<!-- The chart box always renders — with a trace when there are ≥2 points, as
		     an empty frame (just the baseline grid line) when there's no history.
		     Same footprint either way, so an empty host reads as "no data yet", not
		     a collapsed layout. No fabricated trend when empty. -->
		<svg
			class="w-full h-[clamp(40px,7vh,56px)] block"
			:viewBox="`0 0 ${W} ${H}`"
			preserveAspectRatio="none"
			aria-hidden="true"
		>
			<!-- Paints reference the neutral tokens directly (arbitrary values): the
			     grey scale lives on the ink/surface/outline utilities as text /
			     background, but SVG needs stroke — and frappe-ui's preset does not
			     expose `stroke-outline-*`. The var() forms still flip on [data-theme].
			     Faint hairline grid + a 1px mid-ink polyline on top. No fill. -->
			<line
				class="[stroke:var(--outline-gray-1)] [stroke-width:1] [vector-effect:non-scaling-stroke]"
				x1="0"
				:y1="H / 2"
				:x2="W"
				:y2="H / 2"
			/>
			<polyline
				v-if="pts.length > 1"
				class="fill-none [stroke:var(--ink-gray-6)] [stroke-width:1] [vector-effect:non-scaling-stroke] [stroke-linejoin:round] [stroke-linecap:round]"
				:points="linePoints"
			/>
			<!-- One reading so far: a single dot at its value (warming up), not a
			     fabricated trend. r is in the non-scaling stroke space via a fixed
			     px radius drawn as a tiny circle. -->
			<circle
				v-else-if="dot"
				class="[fill:var(--ink-gray-6)]"
				:cx="dot[0]"
				:cy="dot[1]"
				r="2"
				vector-effect="non-scaling-stroke"
			/>
		</svg>
	</div>
</template>

<script setup>
import { computed } from "vue";
import { scaleSeries } from "../derive.js";

const props = defineProps({
	label: { type: String, required: true },
	points: { type: Array, default: () => [] },
	unit: { type: String, default: "" },
	// Formatter for the headline current value; defaults to a grouped integer.
	format: { type: Function, default: null },
});

const W = 260;
const H = 52;

const nums = computed(() =>
	(props.points || []).filter((n) => typeof n === "number" && !Number.isNaN(n))
);

// The trace is drawn only for ≥2 points (one point isn't a trend). padY=3 keeps
// the line off the top/bottom edges.
const pts = computed(() => (nums.value.length < 2 ? [] : scaleSeries(nums.value, W, H, 3)));

// Exactly one point (the first live poll of a rate series, before it has a delta
// to plot): draw a single dot at its value so the chart reads "one reading so
// far", distinct from a bare "no data yet" frame — the warming-up state made
// legible without fabricating a trend. scaleSeries centres a lone point at W/2.
const dot = computed(() => (nums.value.length === 1 ? scaleSeries(nums.value, W, H, 3)[0] : null));

// Straight-segment polyline — the raw sampled shape, no smoothing. A flat
// "x,y x,y …" points list; the <polyline> joins them with 1px segments.
const linePoints = computed(() =>
	pts.value.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ")
);

const current = computed(() => {
	const v = nums.value;
	if (!v.length) return "—";
	const last = v[v.length - 1];
	if (props.format) return props.format(last);
	return Math.round(last).toLocaleString("en-US");
});
</script>
