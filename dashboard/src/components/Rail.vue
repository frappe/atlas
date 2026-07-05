<template>
	<!-- Two-level nav: domains, and the objects inside a domain as indented
	     sub-items. Every object (Addresses, Routes, Snapshots, Units, …) gets its
	     own rail line so you can jump straight to it. Selected is signalled by
	     contrast — --ink text — never by colour. Sub-items appear only for the
	     open domain, and only when it holds more than one object (a lone-table
	     domain stays a single bare panel). -->
	<nav class="flex flex-row flex-wrap gap-1 sm:flex-col sm:gap-px">
		<template v-for="d in domains" :key="d.id">
			<button
				class="item group flex w-auto items-baseline gap-2 border-0 bg-transparent py-2 pl-0 pr-2.5 text-left text-ink-gray-6 cursor-pointer hover:text-ink-gray-8 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ink-gray-9 sm:w-full [&.sel]:text-ink-gray-9"
				:class="{ sel: d.id === domain && !hasSelectedSub(d) }"
				@click="$emit('select', { domain: d.id, table: d.tables?.[0]?.id })"
			>
				<span class="text-base font-normal group-[.sel]:font-medium">{{ d.label }}</span>
				<!-- Counts are hidden by default (they were noise on a calm rail). Only
				     an ACTIONABLE count shows — a domain with firing alerts / dead parts
				     carries its number so the rail itself signals "look here". -->
				<span v-if="d.alert" class="font-mono tabular-nums text-xs text-ink-gray-8">{{
					d.alert
				}}</span>
			</button>

			<template v-if="d.id === domain && d.tables && d.tables.length > 1">
				<button
					v-for="t in d.tables"
					:key="t.id"
					class="sub group flex w-auto items-baseline gap-2 border-0 bg-transparent py-1.5 pl-0 pr-2.5 text-left text-ink-gray-5 cursor-pointer hover:text-ink-gray-8 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ink-gray-9 sm:w-full sm:pl-4 [&.selsub]:text-ink-gray-9"
					:class="{ selsub: t.id === table }"
					@click="$emit('select', { domain: d.id, table: t.id })"
				>
					<span class="text-sm group-[.selsub]:font-medium">{{ t.label }}</span>
					<span v-if="t.alert" class="font-mono tabular-nums text-xs text-ink-gray-8">{{
						t.alert
					}}</span>
				</button>
			</template>
		</template>
	</nav>
</template>

<script setup>
const props = defineProps({
	// [{ id, label, count, tables: [{ id, label, count }] }]
	domains: { type: Array, required: true },
	domain: { type: String, required: true },
	table: { type: String, default: undefined },
});
defineEmits(["select"]);

// A sub-item carries the selection only when the domain is expanded (more than
// one table) and the current table is one of them. In that case the parent
// stays un-highlighted so exactly one rail line reads as selected.
function hasSelectedSub(d) {
	return d.tables && d.tables.length > 1 && d.tables.some((t) => t.id === props.table);
}
</script>
