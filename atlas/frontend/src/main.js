import { createApp } from 'vue'
import { FrappeUI, setConfig, frappeRequest } from 'frappe-ui'

import App from './App.vue'
import router from './router'
import './index.css'

// Route every frappe-ui resource through Frappe's request layer (CSRF,
// session cookie, /api). No raw fetch/axios anywhere in the app.
setConfig('resourceFetcher', frappeRequest)

const app = createApp(App)
app.use(router)
app.use(FrappeUI)
app.mount('#app')
