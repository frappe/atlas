<template>
	<!-- The Overview landing: pressure/quota bars + the size distribution + a
	     "Wants a look" card (firing alerts as plain sentences with jumps). NO
	     deep charts — those live in Analytics. Glanceable, constant size at any
	     VM count. -->
	<div class="grid gap-10">
		<!-- Capacity as the provisioning view (14/11): used vs physical per
		     resource, the overcommit factor, and the shared/dedicated split.
		     Supersedes the old committed-vs-budget bars — it shows the same three
		     resources with the full commit→use→physical truth. -->
		<section class="min-w-0">
			<PanelHead title="Capacity" :summary="capacityNote" h3 />
			<Provisioning :state="state" />
		</section>

		<!-- Wants a look: firing alerts FOLDED by kind — one line per group, count
		     + worst severity + a jump. This keeps the landing constant-size at any
		     VM count (the full list lives on the Alerts page). When clear, one
		     nominal line. -->
		<section class="min-w-0">
			<PanelHead
				title="Wants a look"
				:summary="firingCount ? `${firingCount} firing` : ''"
				h3
			/>
			<div v-if="groups.length" class="flex flex-col">
				<!-- 10-A: no severity dot. Severity reads through the title's ink —
				     crit darkest, warn a step down. The → is the click affordance. -->
				<button
					v-for="g in groups"
					:key="g.key"
					class="group grid grid-cols-[1fr_max-content] items-baseline gap-3 w-full border-0 bg-transparent px-0.5 py-[clamp(7px,1.4vh,11px)] text-left text-ink-gray-7 cursor-pointer hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ink-gray-9 focus-visible:rounded-sm"
					:class="'sev-' + g.severity"
					@click="onGroup(g)"
				>
					<span
						class="text-sm"
						:class="
							g.severity === 'crit'
								? 'text-ink-gray-9'
								: g.severity === 'warn'
								? 'text-ink-gray-7'
								: ''
						"
						>{{ g.count === 1 && g.detail ? g.detail : g.title }}</span
					>
					<span
						class="font-mono tabular-nums text-sm text-ink-gray-5 group-hover:text-ink-gray-9"
						>→</span
					>
				</button>
			</div>
			<p v-else class="m-0 text-sm text-ink-gray-6">{{ clearLine }}</p>
		</section>
	</div>
</template>

<script setup>
import { computed } from "vue";
import Provisioning from "./Provisioning.vue";
import PanelHead from "./PanelHead.vue";
import { alerts, alertGroups } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});
const emit = defineEmits(["open-vm", "open-alerts"]);

const firingCount = computed(() => alerts(props.state).firing.length);
const groups = computed(() => alertGroups(props.state));

// A singular group jumps to its VM; a multi-machine group opens the Alerts page.
function onGroup(g) {
	if (g.vm) emit("open-vm", g.vm);
	else emit("open-alerts");
}

// The over-provision factor is the host's headline promise-vs-have ratio.
const capacityNote = computed(() => {
	const f = props.state.host?.overprovision_factor;
	return f && f !== 1 ? `overprovision ×${f}` : "used vs physical";
});

const clearLine = computed(() => {
	const vms = props.state.virtual_machines || [];
	const running = vms.filter((v) => v.state === "Running").length;
	return `${running} of ${vms.length} running nominally. Nothing wants a look.`;
});
</script>
