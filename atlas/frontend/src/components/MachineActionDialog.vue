<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { Dialog, FormControl, Button, ErrorMessage, toast } from 'frappe-ui'

import { useMachineDoctype } from '../data/machines'

// The form-input lifecycle actions: Snapshot (name a snapshot) and Resize
// (grow the machine). Input-less destructive actions (Rebuild, Terminate) are
// confirms on the Machine page, not this dialog.
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kind: { type: String, default: '' }, // 'snapshot' | 'resize'
  machine: { type: String, required: true },
  doc: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue', 'done'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const vm = useMachineDoctype()

const TITLES = { snapshot: 'Snapshot', resize: 'Resize' }
const HINTS = {
  snapshot: 'Copies the whole disk — up to a few minutes.',
  resize: 'Grows the disk and rewrites the machine config. Disk can only grow.',
}

const form = reactive({ title: '', vcpus: 0, memory_megabytes: 0, disk_gigabytes: 0 })
const error = ref('')

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen) return
    error.value = ''
    form.title = ''
    form.vcpus = props.doc.vcpus ?? 1
    form.memory_megabytes = props.doc.memory_megabytes ?? 512
    form.disk_gigabytes = props.doc.disk_gigabytes ?? 4
  },
)

function argsFor() {
  if (props.kind === 'snapshot') return { title: form.title }
  if (props.kind === 'resize')
    return {
      vcpus: form.vcpus,
      memory_megabytes: form.memory_megabytes,
      disk_gigabytes: form.disk_gigabytes,
    }
  return {}
}

async function submit() {
  error.value = ''
  try {
    await vm.runDocMethod.submit({
      name: props.machine,
      method: props.kind,
      params: argsFor(),
    })
    toast.success(`${TITLES[props.kind]} started`)
    emit('done')
  } catch (e) {
    error.value = vm.runDocMethod.error?.message || e.messages?.[0] || e.message || 'Action failed'
  }
}
</script>

<template>
  <Dialog v-model="open" :options="{ title: TITLES[kind] || 'Action' }">
    <template #body-content>
      <form class="space-y-4" @submit.prevent="submit">
        <p class="text-sm text-ink-gray-5">{{ HINTS[kind] }}</p>

        <FormControl
          v-if="kind === 'snapshot'"
          v-model="form.title"
          label="Snapshot name"
          required
        />

        <template v-if="kind === 'resize'">
          <FormControl v-model.number="form.vcpus" type="number" label="vCPU" />
          <FormControl
            v-model.number="form.memory_megabytes"
            type="number"
            label="Memory (MB)"
          />
          <FormControl
            v-model.number="form.disk_gigabytes"
            type="number"
            label="Disk (GB)"
          />
        </template>

        <ErrorMessage :message="error" />
      </form>
    </template>
    <template #actions>
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="open = false" />
        <Button
          variant="solid"
          theme="gray"
          :label="TITLES[kind] || 'Confirm'"
          :loading="vm.runDocMethod.loading"
          @click="submit"
        />
      </div>
    </template>
  </Dialog>
</template>
