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

const columns = [
  { label: 'Name', key: 'title', width: '2fr', getLabel: ({ row }) => row.title || row.image_name },
  { label: 'Disk', key: 'default_disk_gigabytes', width: '8rem', getLabel: ({ row }) => `${row.default_disk_gigabytes} GB` },
  { label: 'Status', key: '_status', type: 'badge', width: '8rem' },
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
