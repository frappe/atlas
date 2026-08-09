<template>
	<!-- The Overview landing: pressure/quota bars + the size distribution + a
	     "Wants a look" card (firing alerts as plain sentences with jumps). NO
	     deep charts — those live in Analytics. Glanceable, constant size at any
	     VM count. -->
	<div class="grid gap-10">
		<!-- Summary: one glanceable census line — the fleet (running / total, plus a
		     migrating clause only when something's in flight) then the object stores.
		     Each noun cross-links to its own rail page. Constant-size at any VM count,
		     and the first thing the landing answers: "what's on this host". -->
		<section class="min-w-0">
			<PanelHead title="Summary" h3 />
			<p class="m-0 text-sm text-ink-gray-5">
				<!-- Machines — running / total, the fleet as the leading fact. -->
				<button :class="linkClass" @click="onJump(inv.machines.jump)">
					<span class="font-mono tabular-nums text-ink-gray-9 font-medium"
						>{{ inv.machines.running }} / {{ inv.machines.total }}</span
					>
					machines running</button
				><!-- Migrating — only when a VM is actually in flight (honest silence at 0). -->
				<template v-if="inv.machines.migrating > 0"
					><span :class="sepClass">·</span
					><span class="font-mono tabular-nums text-ink-gray-9 font-medium">{{
						inv.machines.migrating
					}}</span>
					migrating</template
				><!-- Stores — volumes / snapshots / images / reserved IPs, each a jump. -->
				<template v-for="s in inv.stores" :key="s.label"
					><span :class="sepClass">·</span
					><button :class="linkClass" @click="onJump(s.jump)">
						<span class="font-mono tabular-nums text-ink-gray-9 font-medium">{{
							s.count
						}}</span>
						{{ s.label }}
					</button></template
				>
			</p>
		</section>

		<!-- Capacity as the provisioning view (14/11): used vs physical per
		     resource, the overcommit factor, and the shared/dedicated split.
		     Supersedes the old committed-vs-budget bars — it shows the same three
		     resources with the full commit→use→physical truth. -->
		<section class="min-w-0">
			<PanelHead title="Capacity" h3 />
			<Provisioning :state="state" />
		</section>

		<!-- Wants a look: firing alerts FOLDED by kind — one line per group, count
		     + worst severity + a jump. This keeps the landing constant-size at any
		     VM count (the full list lives on the Alerts page). When clear, one
		     nominal line. -->
		<section class="min-w-0">
			<PanelHead title="Alerts" h3 />
			<div v-if="groups.length" class="flex flex-col">
				<!-- One weight/ink for every line — the list reads as a calm index, not
				     a heat map. Severity is carried by ordering (crit groups sort first),
				     not by per-line contrast. The → is the click affordance. -->
				<button
					v-for="g in groups"
					:key="g.key"
					class="group grid grid-cols-[1fr_max-content] items-baseline gap-3 w-full border-0 bg-transparent px-0 py-[clamp(7px,1.4vh,11px)] text-left text-ink-gray-8 cursor-pointer hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ink-gray-9 focus-visible:rounded-sm"
					@click="onGroup(g)"
				>
					<span class="text-sm text-ink-gray-8">{{
						g.count === 1 && g.detail ? g.detail : g.title
					}}</span>
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
import { alertGroups, inventory } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
});
const emit = defineEmits(["open-vm", "open-alerts", "open"]);

const groups = computed(() => alertGroups(props.state));
const inv = computed(() => inventory(props.state));

// The census nouns are quiet links: the same light ink as "vCPU" in Capacity
// (ink-gray-5), the only affordance a hover-darken (contrast, not colour — the
// dashboard's rule). The count keeps its own darker/mono weight; the noun word
// carries the hover.
const linkClass =
	"border-0 bg-transparent p-0 font-[inherit] text-sm text-ink-gray-5 cursor-pointer hover:text-ink-gray-8 focus-visible:outline-2 focus-visible:outline-offset-[-1px] focus-visible:outline-ink-gray-9 focus-visible:rounded-sm";
const sepClass = "text-ink-gray-3 px-2";

// A noun jump routes to its rail page via the App-level select().
function onJump(jump) {
	if (jump) emit("open", jump);
}

// A singular group jumps to its VM; a multi-machine group opens the Alerts page.
function onGroup(g) {
	if (g.vm) emit("open-vm", g.vm);
	else emit("open-alerts");
}

const clearLine = computed(() => {
	const vms = props.state.virtual_machines || [];
	const running = vms.filter((v) => v.state === "Running").length;
	return `${running} of ${vms.length} running nominally. Nothing wants a look.`;
});
</script>
