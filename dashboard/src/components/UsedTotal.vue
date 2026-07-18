<template>
	<!-- The used / total · pct split, in one place. The live value leads dark
	     (the fact you read), the total trails faint after a hairline slash — the
	     gap between them is the headroom — and an optional percent tails it. Used
	     by the Storage stack read, the Machines CPU/Mem cells, and any quota
	     headline. Mono/tabular so columns of these line up; whether the whole span
	     is mono is left to the caller's cell (it usually already is). -->
	<span>
		<template v-if="used != null && used !== ''">
			<span :class="usedInk">{{ used }}</span>
			<span :class="slashInk">{{ slashSpaced ? " / " : "/" }}</span>
		</template>
		<span :class="totalInk">{{ total }}</span>
		<span v-if="pct != null" :class="pctInk"> · {{ pct }}%</span>
	</span>
</template>

<script setup>
defineProps({
	// Already-formatted strings (fmtGiB, a count, "1.8"): this is a presentation
	// cell, not a calculator — the caller formats, this lays out the split ink.
	used: { type: [String, Number], default: null },
	total: { type: [String, Number], default: "" },
	// Optional trailing percent (a whole number). Null omits it.
	pct: { type: [Number, String], default: null },
	// Ink weights per part, overridable but defaulted to the shared convention:
	// used darkest, total faint, slash/pct faintest. `!` so they win in a cell
	// that has already set a base text colour.
	usedInk: { type: String, default: "text-ink-gray-8" },
	totalInk: { type: String, default: "text-ink-gray-5" },
	slashInk: { type: String, default: "text-ink-gray-5" },
	pctInk: { type: String, default: "text-ink-gray-5" },
	// Storage spaces its slash (" / "); the Machines cells don't. Default: spaced.
	slashSpaced: { type: Boolean, default: true },
});
</script>
