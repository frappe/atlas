import { createApp } from 'vue'
import { FrappeUI, setConfig, frappeRequest } from 'frappe-ui'

import App from './App.vue'
import router from './router'
import { bootSession } from './data/session'
import './index.css'

// Route every frappe-ui resource through Frappe's request layer (CSRF,
// session cookie, /api). No raw fetch/axios anywhere in the app.
setConfig('resourceFetcher', frappeRequest)

// In dev mode there is no Jinja boot block, so fetch the session user + CSRF
// token before mounting — this way the first write the user triggers already
// carries a valid token. No-op in production (boot data is already present).
bootSession().finally(() => {
  const app = createApp(App)
  app.use(router)
  // The dashboard has no realtime features, so skip frappe-ui's socket.io
  // client. Left on, it connects to a hardcoded :9000 (frappe-ui's default,
  // not this bench's 9007) and floods the console with reconnect errors.
  app.use(FrappeUI, { socketio: false })
  app.mount('#app')
})
