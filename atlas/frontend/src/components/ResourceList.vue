<script setup>
// One list, every page. Wraps frappe-ui's ListView so the three resource
// pages (Machines, Images, Snapshots) share the same shape CRM uses: a
// surface-gray-2 header strip, 40px grid rows, aligned columns. The SPA never
// bulk-selects, so the checkbox column is off (selectable: false).
//
// Empty state is ListView's own (options.emptyState) — title, message, and an
// optional action button — so it tracks the library instead of a bespoke
// component.
//
// `!w-full` on <ListView> overrides the inner wrapper's `w-max`: ListView's
// markup is an overflow-x-auto OUTER wrapping a `w-max min-w-full` INNER that
// holds the grid rows, and passes our attrs (`class`) to that INNER. Left as
// `w-max`, a long cell value lets the grid's max-content win and the row
// overflows past the container (the "lopsided" balloon). Forcing the inner to
// `!w-full` pins it to the container's content box so each `Nfr` column
// distributes the *real* width instead of max-content, and Name (2fr) absorbs
// the slack. (! beats the inner's own `w-max` regardless of stylesheet order.)
//
// Columns are plain frappe-ui ListView columns plus an optional `type` we read
// in the #cell slot to render the right thing: 'badge' (StatusBadge),
// 'copy' (CopyText), 'time' (relative time), 'link' (an emit'd row link),
// 'machine' (name + OS subtitle), 'tags' (tag chips), or the default
// truncated text. ListView has no built-in cell types for these, so this slot
// stays ours. Anything ListView already does (getLabel, width, align, prefix)
// still works.
import { computed } from 'vue'
import { Badge, ListView } from 'frappe-ui'

import StatusBadge from './StatusBadge.vue'
import CopyText from './CopyText.vue'
import { relativeTime } from '../data/format'

const props = defineProps({
  columns: { type: Array, required: true },
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  // Row click → route. When set, rows become router-links and hover.
  getRowRoute: { type: Function, default: null },
  // Empty-state copy + optional action button (Button props: label, onClick,
  // variant, theme, iconLeft, …). ListView renders these when rows is empty.
  emptyTitle: { type: String, required: true },
  emptyMessage: { type: String, default: '' },
  emptyAction: { type: Object, default: null },
})

const emit = defineEmits(['link'])

// The two-line 'machine' cell (mark + name + OS subtitle) needs a taller row
// than the default single-line 40px; everything else keeps the CRM row height.
const hasMachineCell = computed(() =>
  props.columns.some((c) => c.type === 'machine'),
)

const options = computed(() => ({
  selectable: false,
  showTooltip: false,
  resizeColumn: false,
  rowHeight: hasMachineCell.value ? 52 : 40,
  getRowRoute: props.getRowRoute,
  emptyState: {
    title: props.emptyTitle,
    description: props.emptyMessage,
    button: props.emptyAction,
  },
}))
</script>

<template>
  <div class="flex-1 overflow-y-auto px-5 py-4">
    <ListView
      :columns="columns"
      :rows="rows"
      :options="options"
      row-key="name"
      class="!w-full"
    >
      <template #cell="{ column, row, item, align }">
        <div v-if="column.type === 'machine'" class="min-w-0">
          <div class="truncate text-base text-ink-gray-9">{{ row.title || row.name }}</div>
          <div class="truncate text-sm text-ink-gray-5">
            {{ row.os?.name }} {{ row.os?.version }}
          </div>
        </div>

        <div v-else-if="column.type === 'tags'" class="flex min-w-0 flex-wrap gap-1">
          <Badge
            v-for="tag in item || []"
            :key="tag"
            variant="subtle"
            theme="gray"
            :label="tag"
          />
          <span v-if="!(item && item.length)" class="text-ink-gray-4">—</span>
        </div>

        <StatusBadge v-else-if="column.type === 'badge'" :status="item" />

        <CopyText v-else-if="column.type === 'copy'" :value="item" />

        <span
          v-else-if="column.type === 'time'"
          class="truncate text-base text-ink-gray-5"
        >
          {{ relativeTime(item) }}
        </span>

        <button
          v-else-if="column.type === 'link'"
          class="truncate text-base text-ink-gray-7 hover:text-ink-gray-9 hover:underline"
          @click.stop.prevent="emit('link', { column, row })"
        >
          {{ column.getLabel ? column.getLabel({ row }) : item }}
        </button>

        <span v-else class="truncate text-base" :class="align">
          {{ column.getLabel ? column.getLabel({ row }) : item }}
        </span>
      </template>
    </ListView>
  </div>
</template>
