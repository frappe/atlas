<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { Badge, Button, Dropdown, toast, confirmDialog } from 'frappe-ui'

import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import CopyText from '../components/CopyText.vue'
import ActivityList from '../components/ActivityList.vue'
import MachineActionDialog from '../components/MachineActionDialog.vue'
import { useMachine, useMachineDoctype, useImages, decorate } from '../data/machines'
import { actionsFor } from '../data/actions'

const props = defineProps({ name: { type: String, required: true } })
const router = useRouter()

const resource = useMachine(props.name)
// Mutations (lifecycle methods + delete) go through the standard doctype
// composable; runDocMethod.loading drives the action buttons' spinners and
// delete auto-evicts the shared docStore + listStore.
const vm = useMachineDoctype()
// decorate() layers on the placeholder display fields (IPv4/private/region,
// tags, bench rollup) the overview cards show but the backend does not store
// yet — see data/machines.js. Drop the wrap once those are real fields.
const doc = computed(() => (resource.doc ? decorate(resource.doc) : {}))

// The VM stores the image's doc name; show its title (e.g. "Ubuntu 24.04
// Server") instead. Images are shared + readable, so the cached list maps it.
const images = useImages()
const imageLabel = computed(() => {
  const match = (images.data ?? []).find((i) => i.name === doc.value.image)
  return match?.title || match?.image_name || doc.value.image
})
const activity = ref(null)
const dialog = ref({ open: false, kind: '', doc: {} })

// While a machine is provisioning (Pending), the provision Task is created by
// a background job that may not have run when the user lands here. Poll the
// doc + its activity until it leaves Pending, so the queued task appears and
// the status flips to Running on its own — no manual refresh.
const TRANSITIONAL = new Set(['Pending'])
let poll = null

function stopPolling() {
  if (poll) {
    clearInterval(poll)
    poll = null
  }
}

watch(
  () => doc.value.status,
  (status) => {
    if (TRANSITIONAL.has(status)) {
      if (!poll) {
        poll = setInterval(() => {
          resource.reload()
          activity.value?.reload()
        }, 4000)
      }
    } else {
      stopPolling()
    }
  },
  { immediate: true },
)

onUnmounted(stopPolling)

const crumbs = computed(() => [
  { label: 'Machines', route: { name: 'Machines' } },
  { label: doc.value.title || props.name },
])

const actions = computed(() => actionsFor(doc.value.status))
const primary = computed(() => actions.value.find((a) => a.kind === 'primary'))
const subtleActions = computed(() => actions.value.filter((a) => a.kind === 'subtle'))

// MB → GB for the resource summary line, matching the standalone's "4 GB".
const memGb = computed(() => Math.round((doc.value.memory_megabytes || 0) / 1024))

// The Specifications card rows. Region is placeholder (see data/machines.js).
const specs = computed(() => [
  { icon: 'lucide-cpu', label: 'Compute', value: `${doc.value.vcpus} vCPU` },
  { icon: 'lucide-memory-stick', label: 'Memory', value: `${memGb.value} GB RAM` },
  { icon: 'lucide-hard-drive', label: 'Disk', value: `${doc.value.disk_gigabytes} GB SSD` },
  {
    icon: 'lucide-globe',
    label: 'Region',
    value: doc.value.region
      ? `${doc.value.region.flag} ${doc.value.region.name}, ${doc.value.region.country}`
      : '—',
  },
])

// The Bench stat strip (placeholder rollup, see data/machines.js).
const benchStats = computed(() => [
  { label: 'Sites', value: doc.value.bench?.sites ?? 0 },
  { label: 'Frappe', value: doc.value.bench?.version ?? '—' },
  { label: 'Uptime', value: doc.value.bench?.uptime ?? '—' },
])

// What each destructive action does, for the danger-zone card.
const DANGER_DESC = {
  Terminate: 'Permanently delete the VM and its disk.',
  Rebuild: 'Wipe the disk and reinstall from the image.',
  Delete: 'Remove the record permanently.',
}
const dangerActions = computed(() =>
  actions.value
    .filter((a) => a.kind === 'danger')
    .map((a) => ({ ...a, description: DANGER_DESC[a.label] || 'This cannot be undone.' })),
)

function openBench() {
  // Placeholder until the bench dashboard URL is a real field.
  toast.info('Opening Bench dashboard…')
}

const menuActions = computed(() =>
  actions.value
    .filter((a) => a.kind === 'action' || a.kind === 'danger')
    .map((a) => ({
      label: a.label,
      theme: a.kind === 'danger' ? 'red' : 'gray',
      onClick: () => run(a),
    })),
)

async function callMethod(method, args = {}) {
  try {
    await vm.runDocMethod.submit({ name: props.name, method, params: args })
    // runDocMethod runs the method but does NOT refetch — pull the new status
    // and the new Task row ourselves.
    await resource.reload()
    activity.value?.reload()
  } catch (e) {
    toast.error(vm.runDocMethod.error?.message || e.messages?.[0] || e.message || 'Action failed')
  }
}

function run(action) {
  if (action.dialog) {
    dialog.value = { open: true, kind: action.dialog, doc: doc.value }
    return
  }
  if (action.method === '__delete__') {
    confirmDelete()
    return
  }
  if (action.kind === 'danger') {
    confirmDanger(action)
    return
  }
  callMethod(action.method)
}

// Destructive lifecycle methods (Terminate, Rebuild) confirm first. The pinned
// frappe-ui's confirmDialog is title + message only (no theme/confirmLabel and
// the button reads "Confirm"), so the action verb lives in the title.
function confirmDanger(action) {
  confirmDialog({
    title: `${action.label} ${doc.value.title || props.name}?`,
    message: 'This cannot be undone.',
    onConfirm: ({ hideDialog }) => {
      callMethod(action.method, action.args ? action.args(doc.value) : {})
      hideDialog()
    },
  })
}

function confirmDelete() {
  confirmDialog({
    title: `Delete ${doc.value.title || props.name}?`,
    message: 'The record is removed permanently.',
    onConfirm: async ({ hideDialog }) => {
      // useDoctype().delete evicts the shared docStore + listStore on success,
      // so the Machines list updates without a manual reload.
      await vm.delete.submit({ name: props.name })
      toast.success('Deleted')
      hideDialog()
      router.push({ name: 'Machines' })
    },
  })
}

function onDialogDone() {
  dialog.value.open = false
  resource.reload()
  activity.value?.reload()
}
</script>

<template>
  <PageHeader :breadcrumbs="crumbs">
    <template #title-suffix>
      <StatusBadge :status="doc.status" />
    </template>
    <template #actions>
      <Button
        v-if="primary"
        variant="solid"
        theme="gray"
        :label="primary.label"
        :loading="vm.runDocMethod.loading"
        @click="run(primary)"
      />
      <Button
        v-for="a in subtleActions"
        :key="a.label"
        :label="a.label"
        :disabled="vm.runDocMethod.loading"
        @click="run(a)"
      />
      <Dropdown v-if="menuActions.length" :options="menuActions">
        <Button icon="lucide-more-horizontal" :disabled="vm.runDocMethod.loading" />
      </Dropdown>
    </template>
  </PageHeader>

  <div class="flex-1 overflow-y-auto px-5 py-5">
    <!-- Tags + OS subtitle, mirroring the standalone detail header. -->
    <div class="mb-5 flex flex-wrap items-center gap-2 text-sm text-ink-gray-5">
      <span class="text-ink-gray-7">{{ imageLabel }}</span>
      <span>·</span>
      <span>
        {{ doc.vcpus }} vCPU · {{ memGb }} GB · {{ doc.disk_gigabytes }} GB SSD
      </span>
      <span>·</span>
      <span>{{ doc.region?.flag }} {{ doc.region?.name }}</span>
      <Badge
        v-for="tag in doc.tags || []"
        :key="tag"
        variant="subtle"
        theme="gray"
        :label="tag"
      />
    </div>

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <!-- Network access -->
      <section class="rounded-lg border border-outline-gray-1 p-4">
        <h2 class="text-base font-medium text-ink-gray-9">Network access</h2>
        <p class="mt-1 text-sm text-ink-gray-5">
          Reachable over IPv6. Connect as <span class="font-mono">root</span>.
        </p>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="flex items-center gap-3 py-2.5">
            <div class="flex w-24 shrink-0 items-center gap-1.5 text-sm text-ink-gray-5">
              IPv6
              <Badge variant="subtle" theme="blue" label="Primary" />
            </div>
            <div class="flex-1"><CopyText :value="doc.ipv6_address" /></div>
          </div>
          <div class="flex items-center gap-3 py-2.5">
            <div class="w-24 shrink-0 text-sm text-ink-gray-5">IPv4</div>
            <div class="flex flex-1 items-center gap-2">
              <CopyText :value="doc.ipv4_address" />
              <span class="text-xs text-ink-gray-4">(legacy)</span>
            </div>
          </div>
          <div class="flex items-center gap-3 py-2.5">
            <div class="w-24 shrink-0 text-sm text-ink-gray-5">Private</div>
            <div class="flex-1"><CopyText :value="doc.private_address" /></div>
          </div>
        </div>
        <div
          class="mt-3 flex items-center justify-between gap-3 rounded-md bg-surface-gray-2 px-3 py-2"
        >
          <code class="truncate font-mono text-sm text-ink-gray-7">
            <span class="text-ink-gray-4">$</span> {{ doc.ssh_command }}
          </code>
          <CopyText :value="doc.ssh_command" hide-text />
        </div>
      </section>

      <!-- Specifications -->
      <section class="rounded-lg border border-outline-gray-1 p-4">
        <h2 class="text-base font-medium text-ink-gray-9">Specifications</h2>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div
            v-for="spec in specs"
            :key="spec.label"
            class="flex items-center gap-3 py-2.5"
          >
            <span :class="[spec.icon, 'size-4 shrink-0 text-ink-gray-5']" aria-hidden="true" />
            <span class="w-24 shrink-0 text-sm text-ink-gray-5">{{ spec.label }}</span>
            <span class="text-base text-ink-gray-9">{{ spec.value }}</span>
          </div>
        </div>
      </section>

      <!-- Bench -->
      <section class="rounded-lg border border-outline-gray-1 p-4">
        <div class="flex items-center gap-3">
          <span class="lucide-box size-5 shrink-0 text-ink-gray-7" aria-hidden="true" />
          <div class="flex-1">
            <div class="text-base font-medium text-ink-gray-9">Bench</div>
            <div class="text-sm text-ink-gray-5">
              Self-managed web interface · {{ doc.bench?.version }}
            </div>
          </div>
          <Badge
            variant="subtle"
            :theme="doc.bench?.status === 'running' ? 'green' : 'gray'"
            :label="doc.bench?.status === 'running' ? 'Online' : 'Offline'"
          />
        </div>
        <div class="mt-4 grid grid-cols-3 gap-2 text-center">
          <div v-for="stat in benchStats" :key="stat.label">
            <div class="text-lg font-medium text-ink-gray-9">{{ stat.value }}</div>
            <div class="text-xs text-ink-gray-5">{{ stat.label }}</div>
          </div>
        </div>
        <Button
          class="mt-4 w-full"
          icon-left="lucide-arrow-up-right"
          label="Open Bench dashboard"
          :disabled="doc.bench?.status !== 'running'"
          @click="openBench"
        />
      </section>

      <!-- Danger zone: the destructive actions the header tucks into Actions ▾,
           each with a description so the consequence is explicit. -->
      <section
        v-if="dangerActions.length"
        class="rounded-lg border border-outline-gray-1 p-4"
      >
        <h2 class="text-base font-medium text-ink-gray-9">Danger zone</h2>
        <div class="mt-2 divide-y divide-outline-gray-1">
          <div
            v-for="a in dangerActions"
            :key="a.label"
            class="flex items-center gap-3 py-3"
          >
            <div class="flex-1">
              <div class="text-base text-ink-red-3">{{ a.label }}</div>
              <div class="text-sm text-ink-gray-5">{{ a.description }}</div>
            </div>
            <Button
              theme="red"
              :label="a.label"
              :disabled="vm.runDocMethod.loading"
              @click="run(a)"
            />
          </div>
        </div>
      </section>
    </div>

    <div class="mt-8">
      <ActivityList ref="activity" :machine="name" :status="doc.status" />
    </div>
  </div>

  <MachineActionDialog
    v-model="dialog.open"
    :kind="dialog.kind"
    :machine="name"
    :doc="doc"
    @done="onDialogDone"
  />
</template>
