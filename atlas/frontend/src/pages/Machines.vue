<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from 'frappe-ui'

import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import CopyText from '../components/CopyText.vue'
import EmptyState from '../components/EmptyState.vue'
import NewMachineDialog from '../components/NewMachineDialog.vue'
import { useMachines } from '../data/machines'
import { relativeTime } from '../data/format'

const router = useRouter()
const machines = useMachines()
const showNew = ref(false)

// The page has exactly one primary "New Machine" at a time: the header button
// when there are machines, the empty-state button when there are none.
const isEmpty = computed(() => !machines.loading && (machines.data?.length ?? 0) === 0)

function open(name) {
  router.push({ name: 'Machine', params: { name } })
}

function onCreated(name) {
  machines.reload()
  open(name)
}
</script>

<template>
  <div class="flex h-full flex-col">
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

    <div class="flex-1 overflow-y-auto px-6 py-4">
      <EmptyState
        v-if="isEmpty"
        icon="lucide-server"
        title="No machines yet"
        message="Create one to get started."
      >
        <template #action>
          <Button
            variant="solid"
            theme="gray"
            icon-left="lucide-plus"
            label="New Machine"
            @click="showNew = true"
          />
        </template>
      </EmptyState>

      <table v-else class="w-full text-base">
        <thead>
          <tr class="border-b border-outline-gray-1 text-left text-sm text-ink-gray-5">
            <th class="py-2 font-normal">Name</th>
            <th class="w-28 py-2 font-normal">Status</th>
            <th class="py-2 font-normal">Address</th>
            <th class="w-24 py-2 text-right font-normal">Updated</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in machines.data"
            :key="row.name"
            class="cursor-pointer border-b border-outline-gray-1 hover:bg-surface-gray-1"
            @click="open(row.name)"
          >
            <td class="py-2.5 text-ink-gray-9">{{ row.title || row.name }}</td>
            <td class="w-28 py-2.5"><StatusBadge :status="row.status" /></td>
            <td class="py-2.5" @click.stop>
              <CopyText :value="row.ipv6_address" />
            </td>
            <td class="w-24 py-2.5 text-right text-sm text-ink-gray-5">
              {{ relativeTime(row.modified) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <NewMachineDialog v-model="showNew" @created="onCreated" />
  </div>
</template>
