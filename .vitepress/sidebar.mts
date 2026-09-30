import type { DefaultTheme } from 'vitepress'

// One sidebar for the whole handbook, in reading order. Prev and next links follow this order,
// so list each page once. The group that holds the current page opens on its own.
const handbook: DefaultTheme.SidebarItem[] = [
	{
		text: 'Start here',
		items: [
			{ text: 'Introduction', link: '/docs/start/what-atlas-is' },
			{ text: 'How a VM is created', link: '/docs/start/how-a-vm-request-works' },
			{ text: 'Architecture', link: '/docs/start/architecture' },
		],
	},
	{
		text: 'Hosts',
		collapsed: true,
		items: [
			{ text: 'Hosts and providers', link: '/docs/region/hosts-and-providers' },
			{ text: 'Regional settings', link: '/docs/region/configuration' },
			{ text: 'Provision a host', link: '/docs/region/' },
			{ text: 'Metal daemon', link: '/docs/region/metald' },
			{ text: 'Host sync', link: '/docs/region/host-sync' },
			{ text: 'Add a provider', link: '/docs/region/provider-guide' },
		],
	},
	{
		text: 'VMs',
		collapsed: true,
		items: [
			{ text: 'Create and manage a VM', link: '/docs/compute/' },
			{ text: 'Choose a host', link: '/docs/compute/placement' },
			{ text: 'Atlas VM records', link: '/docs/compute/vm-records' },
			{ text: 'Apply VM changes', link: '/docs/compute/reconciliation' },
			{ text: 'Run a VM', link: '/docs/compute/runtime' },
			{ text: 'Change SSH keys', link: '/docs/compute/ssh-keys' },
			{ text: 'Open a console', link: '/docs/compute/console' },
			{ text: 'Sleepy VMs', link: '/docs/compute/sleepy-vms' },
			{ text: 'Move a VM', link: '/docs/compute/migration' },
			{ text: 'Move a VM: host steps', link: '/docs/compute/migration-engine' },
		],
	},
	{
		text: 'Networking',
		collapsed: true,
		items: [
			{ text: 'Overview', link: '/docs/networking/' },
			{ text: 'Traffic paths', link: '/docs/networking/traffic' },
			{
				text: 'WG Mesh',
				collapsed: true,
				items: [
					{ text: 'Design and rules', link: '/docs/networking/wg-mesh/' },
					{ text: 'Gateways', link: '/docs/networking/wg-mesh/gateways' },
					{ text: 'Operations', link: '/docs/networking/wg-mesh/operations' },
					{ text: 'Benchmarks', link: '/docs/networking/wg-mesh/benchmarks' },
				],
			},
			{ text: 'Host networking', link: '/docs/networking/host-networking' },
			{ text: 'Public IPs', link: '/docs/networking/public-ips' },
			{
				text: 'HTTP proxy',
				collapsed: true,
				items: [
					{ text: 'Overview', link: '/docs/networking/http-proxy/' },
					{ text: 'Provision a node', link: '/docs/networking/http-proxy/provisioning' },
					{ text: 'OpenResty', link: '/docs/networking/http-proxy/openresty' },
					{ text: 'Control daemon', link: '/docs/networking/http-proxy/control-daemon' },
					{ text: 'High availability', link: '/docs/networking/http-proxy/high-availability' },
					{ text: 'Install', link: '/docs/networking/http-proxy/install' },
				],
			},
			{ text: 'IPv6 router', link: '/docs/networking/ipv6-router' },
		],
	},
	{
		text: 'Images and disks',
		collapsed: true,
		items: [
			{ text: 'Overview', link: '/docs/storage/' },
			{ text: 'Image records', link: '/docs/storage/image-records' },
			{ text: 'Host storage', link: '/docs/storage/host-storage' },
			{ text: 'Host layout', link: '/docs/storage/host-layout' },
		],
	},
	{
		text: 'Regional services',
		collapsed: true,
		items: [
			{ text: 'Service VMs', link: '/docs/region/service-vms' },
			{ text: 'Cargo service', link: '/docs/region/cargo' },
		],
	},
	{
		text: 'APIs and security',
		collapsed: true,
		items: [
			{ text: 'Overview', link: '/docs/interfaces/' },
			{ text: 'Signing keys and tokens', link: '/docs/interfaces/signing-keys' },
			{ text: 'Tenant API', link: '/docs/interfaces/tenant-api' },
			{ text: 'VM state updates', link: '/docs/interfaces/vm-state-updates' },
			{ text: 'Atlas to Metal API', link: '/docs/interfaces/metal-contract' },
			{ text: 'Security', link: '/docs/interfaces/security' },
			{ text: 'API clients', link: '/docs/interfaces/api-clients' },
		],
	},
	{
		text: 'Build and test',
		collapsed: true,
		items: [
			{ text: 'Setup', link: '/docs/develop/' },
			{ text: 'Test region', link: '/docs/develop/test-region' },
			{ text: 'Atlas app', link: '/docs/develop/atlas-app' },
			{ text: 'Metal', link: '/docs/develop/metal' },
			{ text: 'Metal host tests', link: '/docs/develop/metal-testing' },
			{ text: 'HTTP proxy', link: '/docs/develop/http-proxy' },
			{ text: 'WG Mesh', link: '/docs/develop/wg-mesh' },
			{ text: 'Code map', link: '/docs/develop/code-map' },
		],
	},
	{
		text: 'Fix problems',
		collapsed: true,
		items: [
			{ text: 'Find a problem', link: '/docs/operate/find-a-problem' },
			{ text: 'Atlas checks', link: '/docs/operate/atlas' },
			{ text: 'Metal checks', link: '/docs/operate/metal' },
			{ text: 'Incidents', link: '/docs/incidents/' },
		],
	},
	{
		text: 'Reference',
		collapsed: true,
		items: [
			{ text: 'Glossary', link: '/docs/reference/glossary' },
			{ text: 'Status', link: '/docs/reference/status' },
			{
				text: 'Specifications',
				collapsed: true,
				items: [
					{ text: 'Repository', link: '/SPEC' },
					{ text: 'Atlas app', link: '/atlas/SPEC' },
					{ text: 'Metal', link: '/metal/SPEC' },
					{ text: 'HTTP proxy', link: '/services/http-proxy/SPEC' },
					{ text: 'WG Mesh', link: '/services/wg-mesh/SPEC' },
					{ text: 'IPv6 router', link: '/services/ipv6-router/SPEC' },
				],
			},
			{
				text: 'API references',
				items: [
					{ text: 'Atlas API', link: '/api/atlas/', target: '_blank' },
					{ text: 'Metal API', link: '/api/metal/', target: '_blank' },
					{ text: 'Proxy API', link: '/api/http-proxy/', target: '_blank' },
				],
			},
		],
	},
]

export const sidebar: DefaultTheme.Sidebar = {
	'/docs/': handbook,
	'/atlas/': handbook,
	'/metal/': handbook,
	'/services/': handbook,
	'/clients/': handbook,
	'/SPEC': handbook,
	'/llm/': [
		{
			text: 'Review guides',
			items: [
				{ text: 'Agent tooling setup', link: '/llm/' },
				{ text: 'Go review guide', link: '/llm/go-code-review-guide' },
			],
		},
	],
}
