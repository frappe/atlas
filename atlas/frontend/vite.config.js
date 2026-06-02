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
  server: {
    // The frappeui proxy routes /api to http://<request-host>:<webserver_port>,
    // so the SPA must be reached as http://atlas.tests.local:8087 (not
    // localhost) for Frappe to resolve the right site. Vite 5's host check
    // otherwise blocks that hostname.
    host: true,
    allowedHosts: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  build: {
    // Relative to this frontend/ dir → atlas/public/frontend (the package
    // root's public/, sibling of hooks.py), which Frappe serves at
    // /assets/atlas/frontend/. NOT ../atlas/public (that nests one level too
    // deep at atlas/atlas/public, where get_app_path can't find it).
    outDir: '../public/frontend',
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
      // The socket.io client stack and its CJS-only `debug` dependency. With
      // frappe-ui excluded from prebundling these reach the browser as raw
      // CJS; `debug` has no ESM `default` export, so the bare `import debug
      // from 'debug'` crashes the whole app to a blank page. Listing them
      // here makes Vite prebundle each into ESM.
      'engine.io-client',
      'socket.io-client',
      'debug',
    ],
  },
})
