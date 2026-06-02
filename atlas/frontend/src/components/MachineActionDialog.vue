<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { Dialog, FormControl, Button, ErrorMessage, call, toast } from 'frappe-ui'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kind: { type: String, default: '' }, // 'snapshot' | 'rebuild' | 'resize'
  machine: { type: String, required: true },
  doc: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue', 'done'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const TITLES = { snapshot: 'Snapshot', rebuild: 'Rebuild', resize: 'Resize' }
const HINTS = {
  snapshot: 'Copies the whole disk — up to a few minutes.',
  rebuild: 'Replaces the disk in place. This cannot be undone.',
  resize: 'Grows the disk and rewrites the machine config. Disk can only grow.',
}

const form = reactive({ title: '', vcpus: 0, memory_megabytes: 0, disk_gigabytes: 0 })
const saving = ref(false)
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
  if (props.kind === 'rebuild') return { source_type: 'image', source: props.doc.image }
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
  saving.value = true
  try {
    await call('run_doc_method', {
      dt: 'Virtual Machine',
      dn: props.machine,
      method: props.kind,
      args: JSON.stringify(argsFor()),
    })
    toast.success(`${TITLES[props.kind]} started`)
    emit('done')
  } catch (e) {
    error.value = e.messages?.[0] || e.message || 'Action failed'
  } finally {
    saving.value = false
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
          :loading="saving"
          @click="submit"
        />
      </div>
    </template>
  </Dialog>
</template>
