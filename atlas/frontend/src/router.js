import { createRouter, createWebHistory } from 'vue-router'

import AppShell from './AppShell.vue'

const routes = [
  {
    path: '/',
    component: AppShell,
    children: [
      { path: '', redirect: { name: 'Machines' } },
      {
        path: 'machines',
        name: 'Machines',
        component: () => import('./pages/Machines.vue'),
      },
      {
        path: 'machines/:name',
        name: 'Machine',
        component: () => import('./pages/Machine.vue'),
        props: true,
      },
      {
        path: 'images',
        name: 'Images',
        component: () => import('./pages/Images.vue'),
      },
      {
        path: 'snapshots',
        name: 'Snapshots',
        component: () => import('./pages/Snapshots.vue'),
      },
    ],
  },
]

const router = createRouter({
  // Frappe serves the SPA under /dashboard; vue-router owns the sub-paths.
  history: createWebHistory('/dashboard'),
  routes,
})

export default router
