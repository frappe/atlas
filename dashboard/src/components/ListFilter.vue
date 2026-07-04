<template>
	<!-- The shared list filter toolbar: a free-text search + facet chips + a
	     Filter▾ menu. Extracted from the Machines table so any ListView can turn
	     on search/facets by passing `facets` and binding v-model:query /
	     v-model:facets. Quiet until used — chips only appear once a filter is
	     active, the menu only opens on click. -->
	<div class="flex items-center gap-2 flex-wrap justify-end">
		<span
			v-if="countLabel"
			class="text-xs text-ink-gray-6 tabular-nums whitespace-nowrap font-mono"
			>{{ countLabel }}</span
		>

		<button
			v-for="f in activeChips"
			:key="f.key"
			class="inline-flex items-center gap-1.5 bg-transparent border-0 rounded-none p-0 whitespace-nowrap font-mono tabular-nums text-2xs text-ink-gray-8 cursor-pointer hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 group/chip"
			:title="`Remove ${f.label} filter`"
			@click="toggleFacet(f.key)"
		>
			{{ f.label
			}}<span class="text-2xs text-ink-gray-5 group-hover/chip:text-ink-gray-9">✕</span>
		</button>
		<button
			v-if="query.trim()"
			class="inline-flex items-center gap-1.5 bg-transparent border-0 rounded-none p-0 whitespace-nowrap font-mono tabular-nums text-2xs text-ink-gray-8 cursor-pointer hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 group/chip"
			title="Clear search"
			@click="$emit('update:query', '')"
		>
			“{{ query.trim() }}”<span
				class="text-2xs text-ink-gray-5 group-hover/chip:text-ink-gray-9"
				>✕</span
			>
		</button>

		<div class="relative" :class="{ open: menuOpen }">
			<button
				class="inline-flex items-center gap-1 bg-transparent border-0 text-xs cursor-pointer px-0.5 py-0.5 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
				:class="menuOpen ? 'text-ink-gray-9' : 'text-ink-gray-6 hover:text-ink-gray-9'"
				aria-haspopup="true"
				:aria-expanded="menuOpen"
				@click="toggleMenu"
			>
				Filter
			</button>
			<div
				v-if="menuOpen"
				class="absolute top-[calc(100%+6px)] right-0 z-[15] w-[260px] bg-surface-base border border-outline-gray-1 rounded shadow-md p-2.5"
				@click.stop
			>
				<input
					ref="searchEl"
					:value="query"
					class="w-full box-border bg-transparent border-0 border-b border-outline-gray-1 font-mono tabular-nums text-xs text-ink-gray-8 pt-1 px-0.5 pb-1.5 mb-2 placeholder:text-ink-gray-5 focus:outline-none focus:border-b-ink-gray-9 [&::-webkit-search-cancel-button]:appearance-none [&::-webkit-search-decoration]:appearance-none"
					type="search"
					:placeholder="placeholder"
					aria-label="Filter list"
					@input="$emit('update:query', $event.target.value)"
					@keydown.escape="menuOpen = false"
				/>
				<div class="flex flex-col gap-px">
					<button
						v-for="f in facets"
						:key="f.key"
						class="flex items-baseline justify-between gap-3 bg-transparent border-0 text-xs cursor-pointer px-1.5 py-1.5 rounded-2 text-left hover:bg-surface-gray-2 hover:text-ink-gray-9"
						:class="isActive(f.key) ? 'text-ink-gray-9' : 'text-ink-gray-7'"
						@click="toggleFacet(f.key)"
					>
						<span
							:class="
								isActive(f.key)
									? 'before:content-[\'✓_\'] before:text-ink-gray-6'
									: ''
							"
							>{{ f.label }}</span
						>
						<span class="text-2xs text-ink-gray-5 tabular-nums font-mono">{{
							f.count
						}}</span>
					</button>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";

const props = defineProps({
	// [{ key, label, count }] — a facet is offered only when its count > 0.
	facets: { type: Array, default: () => [] },
	// Bound search string.
	query: { type: String, default: "" },
	// Active facet keys (a Set).
	active: { type: Object, default: () => new Set() },
	// Left-of-chips count line (e.g. "12 of 400 running"). Optional.
	countLabel: { type: String, default: "" },
	placeholder: { type: String, default: "type to filter…" },
});
const emit = defineEmits(["update:query", "toggle-facet"]);

const searchEl = ref(null);
const menuOpen = ref(false);

const activeChips = computed(() => props.facets.filter((f) => props.active.has(f.key)));
const isActive = (key) => props.active.has(key);

function toggleFacet(key) {
	emit("toggle-facet", key);
}
function toggleMenu() {
	menuOpen.value = !menuOpen.value;
	if (menuOpen.value) nextTick(() => searchEl.value?.focus());
}
function onDocClick(e) {
	if (!e.target.closest?.(".relative")) menuOpen.value = false;
}
watch(menuOpen, (open) => {
	if (open) nextTick(() => document.addEventListener("click", onDocClick));
	else document.removeEventListener("click", onDocClick);
});
</script>
