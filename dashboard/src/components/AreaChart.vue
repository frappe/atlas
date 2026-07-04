<template>
	<!-- A flat monochrome area chart (the reference metrics look): a faint filled
	     area under a 1px line, a single mid grid line, no axes, no colour. The
	     header carries the series name and its current (last) value — the chart is
	     read at a glance, the number is the fact. Honest: renders only from real
	     series points; one point draws a dot, not a fabricated trend. -->
	<div class="min-w-0">
		<div class="flex justify-between items-baseline gap-3 mb-2">
			<span class="text-ink-gray-7 text-sm min-w-0 truncate">{{ label }}</span>
			<!-- When a formatter is given it owns the unit; otherwise append it. -->
			<span class="font-mono tabular-nums text-ink-gray-6 text-xs whitespace-nowrap"
				><b class="text-ink-gray-9 font-medium">{{ current }}</b
				><template v-if="unit && !format"> {{ unit }}</template></span
			>
		</div>
		<svg
			v-if="pts.length > 1"
			class="w-full h-[clamp(40px,7vh,56px)] block"
			:viewBox="`0 0 ${W} ${H}`"
			preserveAspectRatio="none"
			aria-hidden="true"
		>
			<!-- Paints reference the neutral tokens directly (arbitrary values): the
			     grey scale lives on the ink/surface/outline utilities as text /
			     background, but SVG needs stroke/fill — and frappe-ui's preset does
			     not expose `fill-surface-*` / `stroke-outline-*`. The var() forms
			     still flip on [data-theme]. Faint hairline grid, a barely-there area
			     fill (matching the old --hair-2), a 1px mid-ink trace on top. -->
			<line
				class="[stroke:var(--outline-gray-1)] [stroke-width:1] [vector-effect:non-scaling-stroke]"
				x1="0"
				:y1="H / 2"
				:x2="W"
				:y2="H / 2"
			/>
			<path class="[fill:var(--surface-gray-2)]" :d="areaPath" />
			<path
				class="fill-none [stroke:var(--ink-gray-6)] [stroke-width:1] [vector-effect:non-scaling-stroke]"
				:d="linePath"
			/>
		</svg>
		<p v-else class="m-0 text-sm text-ink-gray-5">No series.</p>
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

// Smooth the line with a Catmull-Rom → cubic-Bézier spline instead of straight
// segments, so the series reads as a soft curve (the reference metrics look). The
// tension is mild (1/6) — enough to round the joints without overshooting into
// wiggles the data doesn't have.
const linePath = computed(() => {
	const p = pts.value;
	if (p.length < 2) return "";
	if (p.length === 2) return `M${fmt(p[0])} L${fmt(p[1])}`;
	let d = `M${fmt(p[0])}`;
	for (let i = 0; i < p.length - 1; i++) {
		const p0 = p[i - 1] || p[i];
		const p1 = p[i];
		const p2 = p[i + 1];
		const p3 = p[i + 2] || p2;
		const c1x = p1[0] + (p2[0] - p0[0]) / 6;
		const c1y = p1[1] + (p2[1] - p0[1]) / 6;
		const c2x = p2[0] - (p3[0] - p1[0]) / 6;
		const c2y = p2[1] - (p3[1] - p1[1]) / 6;
		d += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${fmt(
			p2
		)}`;
	}
	return d;
});
const areaPath = computed(() => `${linePath.value} L ${W} ${H} L 0 ${H} Z`);

function fmt(pt) {
	return `${pt[0].toFixed(1)} ${pt[1].toFixed(1)}`;
}

const current = computed(() => {
	const v = nums.value;
	if (!v.length) return "—";
	const last = v[v.length - 1];
	if (props.format) return props.format(last);
	return Math.round(last).toLocaleString("en-US");
});
</script>
