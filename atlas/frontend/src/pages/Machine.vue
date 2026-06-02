<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Dropdown, Breadcrumbs, call, toast } from 'frappe-ui'

import StatusBadge from '../components/StatusBadge.vue'
import CopyText from '../components/CopyText.vue'
import ActivityList from '../components/ActivityList.vue'
import MachineActionDialog from '../components/MachineActionDialog.vue'
import { useMachine } from '../data/machines'
import { actionsFor } from '../data/actions'

const props = defineProps({ name: { type: String, required: true } })
const router = useRouter()

const resource = useMachine(props.name)
const doc = computed(() => resource.doc ?? {})
const activity = ref(null)
const busy = ref(false)
const dialog = ref({ open: false, kind: '', doc: {} })

const crumbs = computed(() => [
  { label: 'Machines', route: { name: 'Machines' } },
  { label: doc.value.title || props.name },
])

const actions = computed(() => actionsFor(doc.value.status))
const primary = computed(() => actions.value.find((a) => a.kind === 'primary'))
const subtleActions = computed(() => actions.value.filter((a) => a.kind === 'subtle'))
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
  busy.value = true
  try {
    await call('run_doc_method', {
      dt: 'Virtual Machine',
      dn: props.name,
      method,
      args: JSON.stringify(args),
    })
    await resource.reload()
    activity.value?.reload()
  } catch (e) {
    toast.error(e.messages?.[0] || e.message || 'Action failed')
  } finally {
    busy.value = false
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

function confirmDanger(action) {
  import('frappe-ui').then(({ dialog: d }) => {
    d.confirm({
      title: `${action.label} ${doc.value.title || props.name}?`,
      message: 'This cannot be undone.',
      theme: 'red',
      confirmLabel: action.label,
      onConfirm: ({ close }) => {
        callMethod(action.method)
        close()
      },
    })
  })
}

function confirmDelete() {
  import('frappe-ui').then(({ dialog: d }) => {
    d.confirm({
      title: `Delete ${doc.value.title || props.name}?`,
      message: 'The record is removed permanently.',
      theme: 'red',
      confirmLabel: 'Delete',
      onConfirm: async ({ close }) => {
        await call('frappe.client.delete', { doctype: 'Virtual Machine', name: props.name })
        toast.success('Deleted')
        close()
        router.push({ name: 'Machines' })
      },
    })
  })
}

function onDialogDone() {
  dialog.value.open = false
  resource.reload()
  activity.value?.reload()
}
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="border-b border-outline-gray-1 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <Breadcrumbs :items="crumbs" />
          <div class="mt-2 flex items-center gap-2">
            <h1 class="text-lg text-ink-gray-9">{{ doc.title || name }}</h1>
            <StatusBadge :status="doc.status" />
          </div>
        </div>
        <div class="flex items-center gap-2">
          <Button
            v-if="primary"
            variant="solid"
            theme="gray"
            :label="primary.label"
            :loading="busy"
            @click="run(primary)"
          />
          <Button
            v-for="a in subtleActions"
            :key="a.label"
            :label="a.label"
            :disabled="busy"
            @click="run(a)"
          />
          <Dropdown v-if="menuActions.length" :options="menuActions">
            <Button icon="lucide-more-horizontal" :disabled="busy" />
          </Dropdown>
        </div>
      </div>
    </header>

    <div class="flex-1 overflow-y-auto px-6 py-5">
      <dl class="space-y-2.5 text-base">
        <div class="flex">
          <dt class="w-28 shrink-0 text-ink-gray-5">Address</dt>
          <dd class="text-ink-gray-9"><CopyText :value="doc.ipv6_address" /></dd>
        </div>
        <div class="flex">
          <dt class="w-28 shrink-0 text-ink-gray-5">Resources</dt>
          <dd class="text-ink-gray-9">
            {{ doc.vcpus }} vCPU · {{ doc.memory_megabytes }} MB · {{ doc.disk_gigabytes }} GB
          </dd>
        </div>
        <div class="flex">
          <dt class="w-28 shrink-0 text-ink-gray-5">Image</dt>
          <dd class="text-ink-gray-9">{{ doc.image }}</dd>
        </div>
        <div class="flex">
          <dt class="w-28 shrink-0 text-ink-gray-5">SSH</dt>
          <dd class="text-ink-gray-9"><CopyText :value="doc.ssh_command" /></dd>
        </div>
      </dl>

      <div class="mt-8">
        <ActivityList ref="activity" :machine="name" />
      </div>
    </div>

    <MachineActionDialog
      v-model="dialog.open"
      :kind="dialog.kind"
      :machine="name"
      :doc="doc"
      @done="onDialogDone"
    />
  </div>
</template>
