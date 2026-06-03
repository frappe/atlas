<script setup>
import { computed } from 'vue'

import PageHeader from '../components/PageHeader.vue'
import ResourceList from '../components/ResourceList.vue'
import { useImages } from '../data/machines'

const images = useImages()

// Images are operator-built and shared; for a user this page is read-only.
const rows = computed(() =>
  (images.data ?? []).map((row) => ({
    ...row,
    _status: row.is_active ? 'Active' : 'Stopped',
  })),
)

// Proportional columns (`minmax(content-floor, fr)`) so they spread evenly
// instead of pinning Name to a `2fr` share that leaves a dead gutter mid-row
// with Disk/Status clustered on the right edge. ResourceList forces the
// ListView inner to `!w-full` so these `fr` units distribute the real container
// width instead of max-content (see the note there); the rem floors stop any
// column collapsing below its content when the viewport narrows.
const columns = [
  { label: 'Name', key: 'title', width: 'minmax(14rem, 2fr)', getLabel: ({ row }) => row.title || row.image_name },
  { label: 'Disk', key: 'default_disk_gigabytes', width: 'minmax(8rem, 1fr)', getLabel: ({ row }) => `${row.default_disk_gigabytes} GB` },
  { label: 'Status', key: '_status', type: 'badge', width: 'minmax(8rem, 1fr)' },
]
</script>

<template>
  <PageHeader title="Images" />

  <ResourceList
    :columns="columns"
    :rows="rows"
    :loading="images.loading"
    empty-title="No images available"
    empty-message="Your operator publishes the base images you can use."
  />
</template>
