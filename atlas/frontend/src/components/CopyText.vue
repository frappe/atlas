<script setup>
import { ref } from 'vue'
import { toast } from 'frappe-ui'

const props = defineProps({
  value: { type: String, default: '' },
})

const copied = ref(false)

function copy() {
  if (!props.value) return
  navigator.clipboard.writeText(props.value).then(() => {
    copied.value = true
    toast.success('Copied')
    setTimeout(() => (copied.value = false), 1500)
  })
}
</script>

<template>
  <button
    v-if="value"
    type="button"
    class="inline-flex items-center gap-1.5 text-ink-gray-7 hover:text-ink-gray-9"
    @click="copy"
  >
    <span class="font-mono text-sm">{{ value }}</span>
    <span
      :class="[copied ? 'lucide-check' : 'lucide-copy', 'size-3.5']"
      aria-hidden="true"
    />
  </button>
  <span v-else class="text-ink-gray-4">—</span>
</template>
