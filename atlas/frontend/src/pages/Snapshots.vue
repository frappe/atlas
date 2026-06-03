<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import PageHeader from '../components/PageHeader.vue'
import ResourceList from '../components/ResourceList.vue'
import { useSnapshots, useMachines } from '../data/machines'
import { gigabytes } from '../data/format'

const router = useRouter()
const snapshots = useSnapshots()
// Both lists are owner-scoped by the backend; reuse the cached machines list to
// show each snapshot's machine by its title instead of its opaque name.
const machines = useMachines()
const titleByName = computed(() =>
  Object.fromEntries((machines.data ?? []).map((m) => [m.name, m.title || m.name])),
)

const rows = computed(() => snapshots.data ?? [])

// Proportional columns (`minmax(content-floor, fr)`) so they spread evenly
// instead of leaving Size/Status crammed at the right edge while Name/Machine
// take all the width. ResourceList forces the ListView inner to `!w-full` so
// these `fr` units distribute the real container width instead of max-content
// (see the note there); the rem floors stop any column collapsing below its
// content when the viewport narrows.
const columns = [
  { label: 'Name', key: 'title', width: 'minmax(12rem, 2fr)' },
  {
    label: 'Machine',
    key: 'virtual_machine',
    type: 'link',
    width: 'minmax(12rem, 2fr)',
    getLabel: ({ row }) => titleByName.value[row.virtual_machine] || row.virtual_machine,
  },
  { label: 'Size', key: 'size_bytes', width: 'minmax(8rem, 1fr)', getLabel: ({ row }) => gigabytes(row.size_bytes) },
  { label: 'Status', key: 'status', type: 'badge', width: 'minmax(8rem, 1fr)' },
]

// Each row's Machine cell links back to its VM — the snapshot was created from
// the machine's own page, so that's the one mental model.
function onLink({ row }) {
  router.push({ name: 'Machine', params: { name: row.virtual_machine } })
}
</script>

<template>
  <PageHeader title="Snapshots" />

  <ResourceList
    :columns="columns"
    :rows="rows"
    :loading="snapshots.loading"
    empty-title="No snapshots yet"
    empty-message="Snapshot a stopped machine from its page."
    @link="onLink"
  />
</template>
