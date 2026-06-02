<script setup>
import { useRouter } from 'vue-router'

import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import { useSnapshots } from '../data/machines'
import { gigabytes } from '../data/format'

const router = useRouter()
const snapshots = useSnapshots()

function openMachine(name) {
  router.push({ name: 'Machine', params: { name } })
}
</script>

<template>
  <div class="flex h-full flex-col">
    <PageHeader title="Snapshots" />

    <div class="flex-1 overflow-y-auto px-6 py-4">
      <EmptyState
        v-if="!snapshots.loading && (snapshots.data?.length ?? 0) === 0"
        icon="lucide-camera"
        title="No snapshots yet"
        message="Snapshot a stopped machine from its page."
      />

      <table v-else class="w-full text-base">
        <thead>
          <tr class="border-b border-outline-gray-1 text-left text-sm text-ink-gray-5">
            <th class="py-2 font-normal">Name</th>
            <th class="py-2 font-normal">Machine</th>
            <th class="w-24 py-2 font-normal">Size</th>
            <th class="w-28 py-2 font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in snapshots.data"
            :key="row.name"
            class="border-b border-outline-gray-1"
          >
            <td class="py-2.5 text-ink-gray-9">{{ row.title }}</td>
            <td class="py-2.5">
              <button
                class="text-ink-gray-7 hover:text-ink-gray-9 hover:underline"
                @click="openMachine(row.virtual_machine)"
              >
                {{ row.virtual_machine }}
              </button>
            </td>
            <td class="w-24 py-2.5 text-ink-gray-7">{{ gigabytes(row.size_bytes) }}</td>
            <td class="w-28 py-2.5"><StatusBadge :status="row.status" /></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
