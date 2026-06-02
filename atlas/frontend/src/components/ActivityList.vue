<script setup>
import StatusBadge from './StatusBadge.vue'
import { useMachineTasks } from '../data/machines'
import { relativeTime } from '../data/format'

const props = defineProps({
  machine: { type: String, required: true },
})

// The VM's own Tasks, inline. Tasks have no nav home — the backend permission
// query + has_permission scope this to "tasks of a machine you own".
const tasks = useMachineTasks(props.machine)

defineExpose({ reload: () => tasks.reload() })
</script>

<template>
  <section>
    <h2 class="text-base text-ink-gray-9">Activity</h2>
    <div class="mt-2 border-t border-outline-gray-1">
      <p
        v-if="!tasks.loading && (tasks.data?.length ?? 0) === 0"
        class="py-4 text-sm text-ink-gray-5"
      >
        No activity yet.
      </p>
      <div
        v-for="task in tasks.data"
        :key="task.name"
        class="flex items-center border-b border-outline-gray-1 py-2.5 text-base"
      >
        <div class="w-28 shrink-0"><StatusBadge :status="task.status" /></div>
        <div class="flex-1 font-mono text-sm text-ink-gray-7">{{ task.script }}</div>
        <div class="w-24 shrink-0 text-right text-sm text-ink-gray-5">
          {{ relativeTime(task.creation) }}
        </div>
      </div>
    </div>
  </section>
</template>
