<script setup>
import { h } from 'vue'
import { FrappeUIProvider, Sidebar } from 'frappe-ui'

import { sessionUser, logout } from './data/session'
import atlasLogo from './assets/atlas-logo.svg'

// The pinned frappe-ui (0.1.278) renders a SidebarItem string icon as literal
// text — only the Dropdown path understands `lucide-*` strings. So nav icons
// are passed as tiny components (the <component :is> branch), rendering the
// same lucide span the rest of the app uses. The header menu icons below stay
// plain strings because SidebarHeader feeds them through a Dropdown.
const icon = (name) => () => h('span', { class: [name, 'size-4 text-ink-gray-7'] })

// Three nav items — the user's whole world. No Provider / Server / Task.
// SidebarItem derives its active state from the route, so we only give it `to`.
const sections = [
  {
    items: [
      { label: 'Machines', icon: icon('lucide-server'), to: { name: 'Machines' } },
      { label: 'Images', icon: icon('lucide-disc'), to: { name: 'Images' } },
      { label: 'Snapshots', icon: icon('lucide-camera'), to: { name: 'Snapshots' } },
    ],
  },
]

// Header doubles as the user menu: Atlas mark + the signed-in user + Log out.
// sessionUser is resolved before mount (main.js boots the session in .finally),
// so a static read of .value is settled by the time this shell renders.
const header = {
  title: 'Atlas',
  subtitle: sessionUser.value,
  menuItems: [{ label: 'Log out', icon: 'lucide-log-out', onClick: logout }],
}
</script>

<template>
  <FrappeUIProvider>
    <div class="flex h-screen bg-surface-white text-ink-gray-9">
      <Sidebar :header="header" :sections="sections">
        <!-- Keep the Atlas logo mark in place of SidebarHeader's initial. -->
        <template #header-logo>
          <img :src="atlasLogo" alt="Atlas" class="size-8 rounded object-cover" />
        </template>
      </Sidebar>

      <main class="flex flex-1 flex-col overflow-hidden">
        <router-view />
      </main>
    </div>
  </FrappeUIProvider>
</template>
