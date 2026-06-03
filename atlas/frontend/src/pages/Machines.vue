<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, FormControl } from 'frappe-ui'

import PageHeader from '../components/PageHeader.vue'
import ResourceList from '../components/ResourceList.vue'
import NewMachineDialog from '../components/NewMachineDialog.vue'
import { useMachines } from '../data/machines'

const router = useRouter()
const machines = useMachines()
const showNew = ref(false)
const search = ref('')

const all = computed(() => machines.data ?? [])
// Client-side filter by name or tag — the list is capped at 100 rows, so this
// stays in the page instead of round-tripping a filtered query.
const rows = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return all.value
  return all.value.filter(
    (v) =>
      (v.title || v.name).toLowerCase().includes(q) ||
      (v.tags || []).some((t) => t.includes(q)),
  )
})
// The page has exactly one primary "New Machine" at a time: the header button
// when there are machines, the ListView empty-state button when there are none.
const isEmpty = computed(() => !machines.loading && all.value.length === 0)

// Every column is proportional (`minmax(content-floor, fr)`) so they spread
// evenly across the row instead of pinning Name to a huge `2fr` share that
// leaves a dead gutter mid-row with the data clustered on the right edge. The
// `fr` weights (2 : 1 : 1.3 : 1) keep Name widest while Status/Specs/Updated
// march out to fill the width; the rem floors stop any column collapsing below
// its content when the viewport narrows. ResourceList forces the ListView inner
// to `!w-full` so these `fr` units distribute the real container width instead
// of max-content (see the note there). The IPv6 address (the widest field) and
// tags live on the detail page, not here.
const columns = [
  // 'machine' is read in ResourceList's #cell slot — name + OS subtitle.
  { label: 'Name', key: 'name', type: 'machine', width: 'minmax(12rem, 2fr)' },
  { label: 'Status', key: 'status', type: 'badge', width: 'minmax(6.5rem, 1fr)' },
  {
    label: 'Specs',
    key: 'specs',
    width: 'minmax(11rem, 1.3fr)',
    getLabel: ({ row }) =>
      `${row.vcpus} vCPU · ${Math.round(row.memory_megabytes / 1024)} GB · ${row.disk_gigabytes} GB`,
  },
  { label: 'Updated', key: 'modified', type: 'time', width: 'minmax(7rem, 1fr)', align: 'right' },
]

// The empty-state action, as ListView Button props (rendered by ListEmptyState).
const emptyAction = {
  label: 'New Machine',
  variant: 'solid',
  theme: 'gray',
  iconLeft: 'lucide-plus',
  onClick: () => (showNew.value = true),
}

function rowRoute(row) {
  return { name: 'Machine', params: { name: row.name } }
}

function onCreated(name) {
  machines.reload()
  router.push({ name: 'Machine', params: { name } })
}
</script>

<template>
  <PageHeader title="Machines">
    <template #actions>
      <Button
        v-if="!isEmpty"
        variant="solid"
        theme="gray"
        icon-left="lucide-plus"
        label="New Machine"
        @click="showNew = true"
      />
    </template>
  </PageHeader>

  <div v-if="!isEmpty" class="flex shrink-0 items-center gap-2 px-5 pt-4">
    <FormControl
      v-model="search"
      type="text"
      placeholder="Search by name or tag"
      class="w-64"
    >
      <template #prefix>
        <span class="lucide-search size-4 text-ink-gray-5" aria-hidden="true" />
      </template>
    </FormControl>
    <span class="ml-auto text-sm text-ink-gray-5">
      {{ rows.length }} of {{ all.length }}
    </span>
  </div>

  <ResourceList
    :columns="columns"
    :rows="rows"
    :loading="machines.loading"
    :get-row-route="rowRoute"
    empty-title="No machines yet"
    empty-message="Create one to get started."
    :empty-action="emptyAction"
  />

  <NewMachineDialog v-model="showNew" @created="onCreated" />
</template>
