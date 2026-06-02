<script setup>
import { Badge } from 'frappe-ui'

import { statusTheme } from '../data/status'

defineProps({
  status: { type: String, default: '' },
})
</script>

<template>
  <Badge v-if="status" variant="subtle" :theme="statusTheme(status)" :label="status" />
</template>
