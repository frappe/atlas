// Session bootstrap. In production the Jinja host page (www/dashboard.py →
// jinjaBootData plugin) writes boot values as flat globals before our code
// runs: `window.csrf_token`, `window.user`, `window.site_name`. Under `yarn
// dev` Vite serves the SPA shell raw with no Jinja block, so the user is
// resolved from the session at startup; the CSRF token cannot be fetched over
// the API (no whitelisted endpoint returns it), so dev writes rely on the
// test site's `ignore_csrf`.
import { ref } from 'vue'
import { frappeRequest } from 'frappe-ui'

export const sessionUser = ref(window.user ?? 'Guest')

// Resolve the user before the app mounts when boot data is absent (dev).
// GET, not the frappeRequest default POST: a read needs no CSRF token, which
// is exactly what dev mode lacks. A POST would 400 on the missing token.
export async function bootSession() {
  if (window.user) return
  try {
    const user = await frappeRequest({
      url: '/api/method/frappe.auth.get_logged_user',
      method: 'GET',
    })
    if (user) sessionUser.value = user
  } catch {
    // Stay "Guest" on failure — the dashboard guard already bounced real
    // guests at the front door, so this only hides a transient error.
  }
}

export function logout() {
  // Standard Frappe logout endpoint; redirects to /login afterwards.
  window.location.href = '/api/method/logout'
}
