<script setup>
defineProps({
  title: { type: String, required: true },
})
</script>

<template>
  <header
    class="flex items-center justify-between border-b border-outline-gray-1 px-6 py-4"
  >
    <h1 class="text-lg text-ink-gray-9">{{ title }}</h1>
    <div class="flex gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>
