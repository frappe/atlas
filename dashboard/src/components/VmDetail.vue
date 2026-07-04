<template>
	<!-- The joined VM detail: the VM's scattered rows brought back together.
	     Order (item): Machine state FIRST, then Connectivity. Machine facts are a
	     two-column definition grid with aligned label→value pairs; Connectivity
	     (the packet path) reads full-width below it. -->
	<div class="flex flex-col gap-5 pt-1 pb-1.5">
		<!-- ── Machine facts — aligned two-column definition grid ── -->
		<section>
			<h4 class="text-2xs font-medium tracking-wide uppercase text-ink-gray-5 mt-0 mb-3">
				Machine
			</h4>
			<dl class="grid grid-cols-[repeat(auto-fit,minmax(210px,1fr))] gap-x-11 gap-y-2 m-0">
				<div
					v-for="f in facts"
					:key="f.k"
					class="grid grid-cols-[82px_1fr] gap-3.5 items-baseline min-w-0"
				>
					<dt class="text-xs text-ink-gray-6 whitespace-nowrap">{{ f.k }}</dt>
					<dd
						class="m-0 font-mono tabular-nums text-sm break-all min-w-0"
						:class="[
							!f.v ? 'text-ink-gray-3' : 'text-ink-gray-8',
							f.link ? 'cursor-pointer hover:text-ink-gray-9' : '',
						]"
						@click="f.link && $emit('open-image')"
					>
						{{ f.v || "—" }}
					</dd>
				</div>
			</dl>
		</section>

		<!-- ── Connectivity (full width, below the machine facts) ── -->
		<section>
			<h4 class="text-2xs font-medium tracking-wide uppercase text-ink-gray-5 mt-0 mb-3">
				Connectivity
			</h4>
			<template v-if="path">
				<PacketPath :legs="path" />
				<p v-if="filterSentence" class="mt-3 mb-0 text-sm text-ink-gray-6 leading-relaxed">
					{{ filterSentence }}
				</p>
			</template>
			<p v-else class="m-0 text-sm text-ink-gray-6 max-w-[60ch] leading-relaxed">
				{{ stoppedSentence }}
			</p>
		</section>
	</div>
</template>

<script setup>
import { computed } from "vue";
import PacketPath from "./PacketPath.vue";
import { deriveVm, derivePath, deriveFilterSentence, STOPPED_SENTENCE } from "../derive.js";

const props = defineProps({
	state: { type: Object, required: true },
	vm: { type: Object, required: true },
	uplink: { type: String, default: "eth0" },
});
defineEmits(["open-image"]);

const detail = computed(() => {
	const d = deriveVm(props.state, props.vm);
	d.uplink = props.uplink; // let derivePath name the real masquerade egress iface
	return d;
});
const path = computed(() => derivePath(detail.value));
const filterSentence = computed(() => deriveFilterSentence(props.state, detail.value));
const stoppedSentence = STOPPED_SENTENCE;

// The Machine facet — a definition grid. Labels at --ink-4, values --ink-2 mono.
// The Origin value cross-links out to the Images section (cross-link direction #2).
const facts = computed(() => {
	const d = detail.value;
	const vm = props.vm;
	const snap = d.snapshot;
	return [
		{ k: "Disk", v: vm.disk_lv },
		{ k: "Origin", v: d.diskOrigin === "—" ? "" : d.diskOrigin, link: true },
		{ k: "Data %", v: d.dataPercent != null ? d.dataPercent + "%" : "" },
		{
			k: "Snapshot",
			v: snap ? `${snap.kind}${snap.snapshot_lv ? " · " + snap.snapshot_lv : ""}` : "",
		},
		{ k: "Data disk", v: vm.has_data_disk ? "yes" : "" },
		{ k: "Tap", v: vm.tap_device },
		{ k: "Host veth", v: vm.host_veth || (vm.uuid ? `veth-${vm.uuid.slice(0, 8)}` : "") },
		{ k: "Netns", v: vm.netns },
		{ k: "MAC", v: vm.mac },
		{ k: "Guest v4", v: vm.ipv4_guest },
		{ k: "Unit", v: d.unit ? `${d.unit.active} · ${d.unit.sub}` : "" },
		{ k: "NDP", v: d.ndp ? d.ndp.address : "" },
		{ k: "FC uid", v: vm.fc_uid != null ? String(vm.fc_uid) : "" },
	].filter((f) => f.v || ["Disk", "Origin", "Tap", "Netns", "Unit"].includes(f.k));
});
</script>
