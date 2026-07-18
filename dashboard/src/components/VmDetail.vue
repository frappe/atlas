<template>
	<!-- The joined VM detail: the VM's scattered rows brought back together.
	     Order (item): Machine state FIRST, then Connectivity. Machine facts are a
	     two-column definition grid with aligned label→value pairs; Connectivity
	     (the packet path) reads full-width below it. -->
	<div class="flex flex-col gap-5 pt-1 pb-1.5">
		<!-- ── Machine facts — aligned two-column definition grid ── -->
		<section>
			<div class="flex items-baseline gap-3 mt-0 mb-3">
				<h4 class="text-xs font-medium tracking-wide uppercase text-ink-gray-5 m-0">
					Machine
				</h4>
				<!-- The plumbing (tap / veth / netns / mac / ndp / fc uid) folds behind
				     this toggle — same "+N / hide" idiom as the list VM-row fold. -->
				<button
					v-if="internalCount"
					class="p-0 border-0 bg-transparent text-xs text-ink-gray-5 cursor-pointer font-mono tabular-nums hover:text-ink-gray-8 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
					type="button"
					@click="internals = !internals"
				>
					{{ internals ? "hide internals" : `+${internalCount} internals` }}
				</button>
			</div>
			<dl class="grid grid-cols-[repeat(auto-fit,minmax(210px,1fr))] gap-x-11 gap-y-2 m-0">
				<div
					v-for="f in facts"
					:key="f.k"
					class="grid grid-cols-[82px_1fr] gap-3.5 items-baseline min-w-0"
				>
					<dt class="text-xs text-ink-gray-6 whitespace-nowrap">{{ f.k }}</dt>
					<dd
						class="m-0 font-mono tabular-nums text-sm truncate min-w-0"
						:title="f.v || ''"
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
			<h4 class="text-xs font-medium tracking-wide uppercase text-ink-gray-5 mt-0 mb-3">
				Connectivity
			</h4>
			<template v-if="path">
				<PacketPath :legs="path" />
			</template>
			<p v-else class="m-0 text-sm text-ink-gray-6 max-w-[60ch] leading-relaxed">
				{{ stoppedSentence }}
			</p>
		</section>
	</div>
</template>

<script setup>
import { computed, ref } from "vue";
import PacketPath from "./PacketPath.vue";
import { deriveVm, derivePath, vmIngress, STOPPED_SENTENCE } from "../derive.js";

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
const stoppedSentence = STOPPED_SENTENCE;

// Whether the plumbing facts (tap / veth / netns / mac / ndp / fc uid) are shown.
// Off by default — they're rarely read at a glance; the ~7 an operator acts on
// carry the dock. Folded behind a "+N internals" toggle in the section header.
const internals = ref(false);

// The Machine facet — a definition grid. The Origin value cross-links out to the
// Images section (cross-link direction #2). Split into PRIMARY (always) and
// INTERNAL (behind the toggle): primary is what an operator acts on; internal is
// host-side plumbing surfaced only on demand.
const primaryFacts = computed(() => {
	const d = detail.value;
	const vm = props.vm;
	const snap = d.snapshot;
	// Ingress — the VM's public reachability (a reserved v4 or a proxy SNI). Moved
	// off the Machines scan table (it's a per-VM detail, blank on most rows) into
	// the dock, where the whole packet path already lives below it.
	const ing = vmIngress(props.state, vm);
	return [
		{ k: "Disk", v: vm.disk_lv },
		{ k: "Origin", v: d.diskOrigin === "—" ? "" : d.diskOrigin, link: true },
		{ k: "Data %", v: d.dataPercent != null ? d.dataPercent + "%" : "" },
		{
			k: "Snapshot",
			v: snap ? `${snap.kind}${snap.snapshot_lv ? " · " + snap.snapshot_lv : ""}` : "",
		},
		{ k: "Data disk", v: vm.has_data_disk ? "yes" : "" },
		{ k: "Guest v4", v: vm.ipv4_guest },
		{ k: "Ingress", v: ing ? ing.label : "" },
		{ k: "Unit", v: d.unit ? `${d.unit.active} · ${d.unit.sub}` : "" },
	].filter((f) => f.v || ["Disk", "Origin", "Unit"].includes(f.k));
});

const internalFacts = computed(() => {
	const d = detail.value;
	const vm = props.vm;
	return [
		{ k: "Tap", v: vm.tap_device },
		{ k: "Host veth", v: vm.host_veth || (vm.uuid ? `veth-${vm.uuid.slice(0, 8)}` : "") },
		{ k: "Netns", v: vm.netns },
		{ k: "MAC", v: vm.mac },
		{ k: "NDP", v: d.ndp ? d.ndp.address : "" },
		{ k: "FC uid", v: vm.fc_uid != null ? String(vm.fc_uid) : "" },
	].filter((f) => f.v || ["Tap", "Netns"].includes(f.k));
});

// How many internal facts the toggle would reveal (for its "+N internals" label).
const internalCount = computed(() => internalFacts.value.length);

// The facts actually rendered: primary always, internal appended when expanded.
const facts = computed(() =>
	internals.value ? [...primaryFacts.value, ...internalFacts.value] : primaryFacts.value
);
</script>
