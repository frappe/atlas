import path from 'path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'

// The dashboard SPA. Served by Frappe at /dashboard (see hooks.py
// website_route_rules) and built into atlas/public/frontend. The frappeui
// plugin proxies /api to the running Frappe backend during `yarn dev`,
// generates the production HTML, and resolves frappe-ui's ~icons imports.
export default defineConfig({
  plugins: [
    frappeui({
      frappeProxy: true,
      lucideIcons: true,
      jinjaBootData: true,
      // We do NOT let the plugin write the www host page. The built
      // index.html (with hashed assets + the boot-data block) is read and
      // inlined by atlas/www/dashboard.py at render time, which keeps the
      // route hash-agnostic and lets the page enforce the signed-in guard.
      buildConfig: false,
    }),
    vue(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  build: {
    // Relative to this frontend/ dir → atlas/atlas/public/frontend, which
    // Frappe serves at /assets/atlas/frontend/.
    outDir: '../atlas/public/frontend',
    emptyOutDir: true,
    target: 'es2015',
    sourcemap: true,
  },
  optimizeDeps: {
    // frappe-ui ships unbuilt source with ~icons/lucide/* virtual imports the
    // esbuild prebundler cannot resolve. Skip prebundling frappe-ui; list its
    // transitive CJS deps so the browser gets ESM.
    exclude: ['frappe-ui'],
    include: [
      'feather-icons',
      'showdown',
      'tailwind.config.js',
      'engine.io-client',
    ],
  },
})
