import { defineConfig } from 'vitepress'
import { posix, resolve } from 'node:path'
import { apiReferencePlugin, writeApiReferences } from './api-reference'
import { sidebar } from './sidebar.mts'

const base = '/atlas/'

export default defineConfig({
	title: 'Atlas',
	description: 'The virtual machine control system for Frappe Cloud V2.',
	base,
	cleanUrls: true,
	lastUpdated: true,
	// A link can point at a script or at a directory that GitHub lists. Neither becomes a page.
	ignoreDeadLinks: [/\.(sh|go|py|toml|h|c)$/, /\/(?:Makefile|authorized-keys-command|apply-metadata)$/, /\/docs\/index$/, /^\/api\//],

	srcExclude: ['README.md', '**/CLAUDE.md', '**/AGENTS.md', 'report.md', 'docs-build/**', '.tmp/**', '**/node_modules/**'],
	markdown: {
		config(markdown) {
			const defaultFence = markdown.renderer.rules.fence
			const defaultLink = markdown.renderer.rules.link_open

			// Source files stay relative in Markdown, but are read on GitHub from the site.
			markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
				const token = tokens[index]
				const href = token.attrGet('href') ?? ''

				// The generated API references are standalone pages outside the VitePress router.
				if (href.startsWith('/api/')) {
					token.attrSet('href', base + href.slice(1))
					token.attrSet('target', '_blank')
					token.attrSet('rel', 'noopener')
				}

				if (!/^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(href)) {
					const sourcePath = posix.resolve('/', posix.dirname(environment.relativePath), href)
					const isSourceFile = /\.(?:go|py|lua|sh|toml|json|ya?ml|h|c)(?:#.*)?$/.test(href)
						|| /\/(?:Makefile|authorized-keys-command|apply-metadata)(?:#.*)?$/.test(href)
					const isSourceDirectory = href.endsWith('/') && /^\/(?:atlas|metal|services|clients)\//.test(sourcePath)

					if (isSourceFile || isSourceDirectory) {
						const kind = isSourceDirectory ? 'tree' : 'blob'
						token.attrSet('href', `https://github.com/frappe/atlas/${kind}/develop${sourcePath}`)
					}
				}

				return defaultLink?.(tokens, index, options, environment, renderer) ?? renderer.renderToken(tokens, index, options)
			}

			markdown.renderer.rules.fence = (tokens, index, options, environment, renderer) => {
				const token = tokens[index]

				if (token.info.trim() === 'mermaid') {
					return `<MermaidDiagram source="${encodeURIComponent(token.content)}" />`
				}

				return defaultFence?.(tokens, index, options, environment, renderer) ?? ''
			}
		},
	},
	rewrites: {
		'docs/index.md': 'index.md',
		':directory(.*)/README.md': ':directory/index.md',
	},

	buildEnd(siteConfig) {
		writeApiReferences(siteConfig.outDir)
	},

	vite: {
		plugins: [apiReferencePlugin(base)],
		// The repository root holds build trees and scratch checkouts; scan only the theme for dependencies.
		optimizeDeps: { entries: ['.vitepress/theme/index.ts'] },
		publicDir: resolve(__dirname, 'public'),
		server: {
			allowedHosts: ['.trycloudflare.com'],

			// Each directory needs a pattern next to its contents pattern, or the walk
			// still enters it. The proxy build root holds a complete chroot.
			watch: {
				followSymlinks: false,
				ignored: [
					'**/.build',
					'**/.build/**',
					'**/.git',
					'**/.git/**',
					'**/.tmp',
					'**/.tmp/**',
					'**/.venv',
					'**/.venv/**',
					'**/.vitepress/dist',
					'**/.vitepress/dist/**',
					'**/node_modules',
					'**/node_modules/**',
				],
			},
		},
	},

	themeConfig: {
		logo: '/logo.svg',
		search: { provider: 'local' },
		outline: { level: 2, label: 'On this page' },

		nav: [
			{ text: 'Start', link: '/docs/start/what-atlas-is', activeMatch: '/docs/start/' },
			{
				text: 'System',
				activeMatch: '/docs/(compute|networking|storage|region)/',
				items: [
					{ text: 'Hosts', link: '/docs/region/hosts-and-providers' },
					{ text: 'VMs', link: '/docs/compute/' },
					{ text: 'Networking', link: '/docs/networking/' },
					{ text: 'Images and disks', link: '/docs/storage/' },
					{ text: 'Regional services', link: '/docs/region/service-vms' },
				],
			},
			{ text: 'APIs', link: '/docs/interfaces/', activeMatch: '/docs/interfaces/' },
			{ text: 'Develop', link: '/docs/develop/', activeMatch: '/docs/develop/' },
			{ text: 'Operate', link: '/docs/operate/find-a-problem', activeMatch: '/docs/(operate|incidents)/' },
			{ text: 'Reference', link: '/docs/reference/glossary', activeMatch: '/docs/reference/|/SPEC' },
		],

		sidebar,

		socialLinks: [{ icon: 'github', link: 'https://github.com/frappe/atlas' }],
		footer: { message: 'AGPL-3.0', copyright: 'Frappe Technologies' },
	},
})
