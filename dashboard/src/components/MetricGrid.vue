<template>
	<!-- A grid of AreaCharts over a metrics series. Overview shows it three-across
	     (compact, glanceable); Analytics two-across (the deep view). Same charts,
	     one `cols` prop is the only difference. -->
	<div
		class="grid min-w-0 gap-[clamp(10px,2vh,22px)_clamp(18px,3vw,34px)] grid-cols-[repeat(var(--cols),minmax(0,1fr))] max-[760px]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] max-[480px]:grid-cols-[minmax(0,1fr)]"
		:style="{ '--cols': cols }"
	>
		<AreaChart
			v-for="m in series"
			:key="m.key"
			:label="m.label"
			:unit="m.unit"
			:points="m.points"
			:format="m.format"
		/>
	</div>
</template>

<script setup>
import AreaChart from "./AreaChart.vue";

defineProps({
	series: { type: Array, default: () => [] },
	cols: { type: Number, default: 3 },
});
</script>
