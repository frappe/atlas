// The logged-in user. Frappe injects boot data into window.frappe via the
// jinja host page (www/dashboard.html); we read the session user from there
// and fall back to a /api call only if boot is absent (e.g. `yarn dev`).
import { computed } from 'vue'

const boot = window.frappe?.boot ?? {}

export const sessionUser = computed(() => boot.user?.name ?? 'Guest')

export function logout() {
  // Standard Frappe logout endpoint; redirects to /login afterwards.
  window.location.href = '/api/method/logout'
}
