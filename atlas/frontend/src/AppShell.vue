<script setup>
import { FrappeUIProvider, Dropdown } from 'frappe-ui'

import { sessionUser, logout } from './data/session'

const nav = [
  { name: 'Machines', label: 'Machines', icon: 'lucide-server' },
  { name: 'Images', label: 'Images', icon: 'lucide-disc' },
  { name: 'Snapshots', label: 'Snapshots', icon: 'lucide-camera' },
]

const userActions = [{ label: 'Log out', icon: 'lucide-log-out', onClick: logout }]
</script>

<template>
  <FrappeUIProvider>
    <div class="flex h-screen bg-surface-white text-ink-gray-9">
      <aside
        class="flex w-56 shrink-0 flex-col border-r border-outline-gray-1 px-2 py-3"
      >
        <div class="px-2 pb-3 text-base font-medium text-ink-gray-9">Atlas</div>

        <nav class="flex flex-1 flex-col gap-1">
          <router-link
            v-for="item in nav"
            :key="item.name"
            :to="{ name: item.name }"
            class="flex items-center gap-2 rounded-md px-2 py-1.5 text-base text-ink-gray-7 hover:bg-surface-gray-2"
            active-class="bg-surface-gray-2 text-ink-gray-9"
          >
            <span :class="[item.icon, 'size-4']" aria-hidden="true" />
            {{ item.label }}
          </router-link>
        </nav>

        <Dropdown :options="userActions" placement="top">
          <button
            class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-base text-ink-gray-7 hover:bg-surface-gray-2"
          >
            <span class="lucide-circle-user size-4" aria-hidden="true" />
            <span class="truncate">{{ sessionUser }}</span>
          </button>
        </Dropdown>
      </aside>

      <main class="flex-1 overflow-y-auto">
        <router-view />
      </main>
    </div>
  </FrappeUIProvider>
</template>
