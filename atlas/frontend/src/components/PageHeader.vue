<script setup>
import { Breadcrumbs } from 'frappe-ui'

// The thin top bar CRM uses (LayoutHeader): a fixed-height row with the
// title/breadcrumb on the left at text-lg font-medium and the page's one
// primary action on the right. Pass either `title` (plain) or `breadcrumbs`
// (frappe-ui items: { label, route }).
defineProps({
  title: { type: String, default: '' },
  breadcrumbs: { type: Array, default: null },
})
</script>

<template>
  <header
    class="flex h-12 shrink-0 items-center justify-between border-b border-outline-gray-1 px-5"
  >
    <div class="flex min-w-0 items-center gap-2">
      <Breadcrumbs v-if="breadcrumbs" :items="breadcrumbs" />
      <h1 v-else class="truncate text-lg font-medium text-ink-gray-9">{{ title }}</h1>
      <slot name="title-suffix" />
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>
