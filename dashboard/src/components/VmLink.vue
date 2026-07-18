<template>
	<!-- The uuid8 cross-link: opens a VM in Machines. (12-C) A bare mono uuid, no
	     arrow — a dotted underline is the click hint, going solid + darker on hover.
	     Falls back to a quiet "host" label when the row isn't a VM. -->
	<button
		v-if="vm"
		class="bg-transparent border-0 p-0 font-mono tabular-nums text-xs leading-[inherit] text-ink-gray-8 cursor-pointer no-underline hover:text-ink-gray-9 focus-visible:outline-2 focus-visible:outline-ink-gray-9 focus-visible:outline-offset-2 focus-visible:rounded-sm"
		@click="$emit('open-vm', vm)"
	>
		{{ uuid8(vm) }}
	</button>
	<span v-else class="font-mono tabular-nums text-xs text-ink-gray-3">host</span>
</template>

<script setup>
import { uuid8 } from "../derive.js";

defineProps({ vm: { type: String, default: null } });
defineEmits(["open-vm"]);
</script>
