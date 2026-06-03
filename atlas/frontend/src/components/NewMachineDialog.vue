<script setup>
import { reactive, ref, computed, watch } from 'vue'
import { Dialog, FormControl, Button, Badge, ErrorMessage, call, toast } from 'frappe-ui'

import { useImages, osBrand, FIXED } from '../data/machines'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'created'])

// The user picks the base image — the shared, read-only Virtual Machine Images
// the operator keeps Active. Server placement stays automatic (filled by the
// controller's before_insert). Only Active images are offered.
const images = useImages()
const imageOptions = computed(() =>
  (images.data ?? [])
    .filter((i) => i.is_active)
    .map((i) => ({ label: i.title || i.image_name || i.name, value: i.name })),
)

// The same options, decorated with OS name/version so the picker renders
// selectable cards. The first active image is tagged "Recommended", mirroring
// the standalone.
const imageCards = computed(() =>
  (images.data ?? [])
    .filter((i) => i.is_active)
    .map((i, idx) => {
      const brand = osBrand(i.image_name || i.name)
      return {
        value: i.name,
        label: i.title || brand.name,
        version: brand.version,
        note: idx === 0 ? 'Recommended' : '',
      }
    }),
)

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

// PLACEHOLDER: per-preset price for the summary panel. The backend has no
// pricing yet — these are display-only, scaled off FIXED.priceMo. Remove with
// the FIXED block in data/machines.js when real pricing lands.
const SIZE_PRICE = {
  'Small (1 vCPU / 512 MB / 4 GB)': FIXED.priceMo / 2,
  'Medium (2 vCPU / 2048 MB / 10 GB)': FIXED.priceMo,
  'Large (4 vCPU / 8192 MB / 40 GB)': FIXED.priceMo * 2.5,
}

const form = reactive({
  title: '',
  image: '',
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

// Summary panel: the chosen size's resources + placeholder price/region.
const selectedSize = computed(() => SIZE_FIELDS[form.size_preset])
const priceMo = computed(() => SIZE_PRICE[form.size_preset] ?? FIXED.priceMo)
const priceHr = computed(() => priceMo.value / (24 * 30))
const region = FIXED.region

// Default to the first available image once they load (and whenever the dialog
// opens with none chosen), so Create is one click in the common single-image
// case while still letting the user switch.
watch(
  [imageOptions, open],
  ([options, isOpen]) => {
    if (isOpen && !form.image && options.length) form.image = options[0].value
  },
  { immediate: true },
)

function reset() {
  form.title = ''
  form.image = imageOptions.value[0]?.value ?? ''
  form.size_preset = SIZES[0].value
  form.ssh_public_key = ''
  error.value = ''
}

async function create() {
  error.value = ''
  creating.value = true
  try {
    // Standard Frappe endpoint: frappe.client.insert. The user chose the
    // image; `server` is omitted — the controller fills it in before_insert,
    // and after_insert auto-provisions, so one Create boots the machine.
    const doc = await call('frappe.client.insert', {
      doc: {
        doctype: 'Virtual Machine',
        title: form.title,
        image: form.image,
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
      <!-- reka-ui's DialogOverlay (which wraps the content) runs a
           `pointerdown.left.prevent` handler to suppress backdrop text
           selection. Because frappe-ui nests the content *inside* the overlay,
           that preventDefault bubbles up and cancels focus for every field —
           left-click won't focus an input (Tab and right-click still work).
           Stopping pointerdown here keeps it from reaching the overlay, so
           clicks inside the form focus normally; backdrop clicks (outside the
           form) still dismiss the dialog. -->
      <form class="space-y-4" @submit.prevent="create" @pointerdown.stop>
        <FormControl v-model="form.title" label="Name" required />

        <!-- Image picker as selectable cards (OS mark + name + version),
             matching the list's OS marks. The grid caps its height and scrolls
             so a long image list never blows out the dialog. -->
        <div>
          <label class="mb-1.5 block text-xs text-ink-gray-5">Image</label>
          <div class="grid max-h-44 grid-cols-2 gap-2 overflow-y-auto">
            <button
              v-for="img in imageCards"
              :key="img.value"
              type="button"
              class="flex min-w-0 items-center gap-2.5 rounded-lg border p-2.5 text-left transition"
              :class="
                form.image === img.value
                  ? 'border-outline-gray-4 ring-1 ring-outline-gray-3'
                  : 'border-outline-gray-2 hover:border-outline-gray-3'
              "
              @click="form.image = img.value"
            >
              <div class="min-w-0 flex-1">
                <div class="truncate text-sm font-medium text-ink-gray-9">{{ img.label }}</div>
                <div class="truncate text-xs text-ink-gray-5">
                  {{ img.version ? `Version ${img.version}` : img.value }}
                </div>
              </div>
              <Badge
                v-if="img.note"
                variant="subtle"
                theme="green"
                :label="img.note"
                class="shrink-0"
              />
            </button>
          </div>
        </div>

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

        <!-- Live summary: what you're about to create. Region + price are
             placeholder (see data/machines.js / SIZE_PRICE). -->
        <div class="rounded-lg border border-outline-gray-2 bg-surface-gray-1 p-3">
          <div class="mb-2 text-xs font-medium text-ink-gray-5">Summary</div>
          <dl class="space-y-1.5 text-sm">
            <div class="flex justify-between">
              <dt class="text-ink-gray-5">Region</dt>
              <dd class="text-ink-gray-9">{{ region.flag }} {{ region.name }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-gray-5">Compute</dt>
              <dd class="text-ink-gray-9">
                {{ selectedSize.vcpus }} vCPU · {{ Math.round(selectedSize.memory_megabytes / 1024 * 10) / 10 }} GB
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-gray-5">Disk</dt>
              <dd class="text-ink-gray-9">{{ selectedSize.disk_gigabytes }} GB SSD</dd>
            </div>
            <div
              class="mt-1 flex items-baseline justify-between border-t border-outline-gray-2 pt-2"
            >
              <dd class="text-base font-medium text-ink-gray-9">
                ${{ priceMo }}<span class="text-sm font-normal text-ink-gray-5"> /mo</span>
              </dd>
              <dd class="font-mono text-xs text-ink-gray-5">≈ ${{ priceHr.toFixed(3) }} / hr</dd>
            </div>
          </dl>
        </div>

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
