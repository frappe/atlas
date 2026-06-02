<script setup>
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import { useImages } from '../data/machines'

const images = useImages()

function statusOf(row) {
  return row.is_active ? 'Active' : 'Stopped'
}
</script>

<template>
  <div class="flex h-full flex-col">
    <PageHeader title="Images" />

    <div class="flex-1 overflow-y-auto px-6 py-4">
      <EmptyState
        v-if="!images.loading && (images.data?.length ?? 0) === 0"
        icon="lucide-disc"
        title="No images available"
        message="Your operator publishes the base images you can use."
      />

      <table v-else class="w-full text-base">
        <thead>
          <tr class="border-b border-outline-gray-1 text-left text-sm text-ink-gray-5">
            <th class="py-2 font-normal">Name</th>
            <th class="w-24 py-2 font-normal">Disk</th>
            <th class="w-28 py-2 font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in images.data"
            :key="row.name"
            class="border-b border-outline-gray-1"
          >
            <td class="py-2.5 text-ink-gray-9">{{ row.title || row.image_name }}</td>
            <td class="w-24 py-2.5 text-ink-gray-7">{{ row.default_disk_gigabytes }} GB</td>
            <td class="w-28 py-2.5"><StatusBadge :status="statusOf(row)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
