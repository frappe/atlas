<script setup>
import { reactive, ref, computed } from 'vue'
import { Dialog, FormControl, Button, ErrorMessage, call, toast } from 'frappe-ui'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'created'])

// Three presets only — no Custom for users. Labels mirror the schema Select
// on Virtual Machine.size_preset; the resource numbers are filled server-side
// by the same size_preset handler the desk form uses.
const SIZES = [
  { label: 'Small', value: 'Small (1 vCPU / 512 MB / 4 GB)', hint: '1 vCPU · 512 MB · 4 GB' },
  { label: 'Medium', value: 'Medium (2 vCPU / 2048 MB / 10 GB)', hint: '2 vCPU · 2048 MB · 10 GB' },
  { label: 'Large', value: 'Large (4 vCPU / 8192 MB / 40 GB)', hint: '4 vCPU · 8192 MB · 40 GB' },
]
const SIZE_FIELDS = {
  'Small (1 vCPU / 512 MB / 4 GB)': { vcpus: 1, memory_megabytes: 512, disk_gigabytes: 4 },
  'Medium (2 vCPU / 2048 MB / 10 GB)': { vcpus: 2, memory_megabytes: 2048, disk_gigabytes: 10 },
  'Large (4 vCPU / 8192 MB / 40 GB)': { vcpus: 4, memory_megabytes: 8192, disk_gigabytes: 40 },
}

const form = reactive({
  title: '',
  size_preset: SIZES[0].value,
  ssh_public_key: '',
})
const creating = ref(false)
const error = ref('')

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const sizeHint = computed(() => SIZES.find((s) => s.value === form.size_preset)?.hint ?? '')

function reset() {
  form.title = ''
  form.size_preset = SIZES[0].value
  form.ssh_public_key = ''
  error.value = ''
}

async function create() {
  error.value = ''
  creating.value = true
  try {
    // Standard Frappe endpoint: frappe.client.insert. server + image are
    // omitted — the Virtual Machine controller fills them in before_insert,
    // and after_insert auto-provisions, so one Create boots the machine.
    const doc = await call('frappe.client.insert', {
      doc: {
        doctype: 'Virtual Machine',
        title: form.title,
        size_preset: form.size_preset,
        ssh_public_key: form.ssh_public_key,
        ...SIZE_FIELDS[form.size_preset],
      },
    })
    toast.success('Machine created')
    open.value = false
    reset()
    emit('created', doc.name)
  } catch (e) {
    error.value = e.messages?.[0] || e.message || 'Could not create the machine'
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog v-model="open" :options="{ title: 'New Machine' }">
    <template #body-content>
      <form class="space-y-4" @submit.prevent="create">
        <FormControl v-model="form.title" label="Name" required />

        <FormControl
          v-model="form.size_preset"
          type="select"
          label="Size"
          :options="SIZES.map((s) => ({ label: s.label, value: s.value }))"
        />
        <p class="-mt-2 text-sm text-ink-gray-5">{{ sizeHint }}</p>

        <FormControl
          v-model="form.ssh_public_key"
          type="textarea"
          label="SSH key"
          required
        />

        <ErrorMessage :message="error" />
      </form>
    </template>
    <template #actions>
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="open = false" />
        <Button
          variant="solid"
          theme="gray"
          label="Create"
          :loading="creating"
          @click="create"
        />
      </div>
    </template>
  </Dialog>
</template>
