import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import type { Plugin } from 'vite'

const root = resolve(__dirname, '..')
const scalarScript = resolve(root, 'node_modules/@scalar/api-reference/dist/browser/standalone.js')

interface ApiReference {
	name: string
	title: string
	specification: string
	server: string
}

// Each API gets a standalone Scalar page under `api/<name>/`, outside the VitePress layout.
const references: ApiReference[] = [
	{ name: 'atlas', title: 'Atlas API', specification: 'clients/openapi/atlas-client.json', server: 'https://atlas.example.com' },
	{ name: 'http-proxy', title: 'HTTP proxy control API', specification: 'clients/openapi/atlas-proxy-client.json', server: 'https://proxy.example.com' },
	{ name: 'wg-gateway', title: 'WireGuard gateway API', specification: 'clients/openapi/atlas-wg-gateway-client.json', server: 'https://wireguard.example.com' },
	{ name: 'metal', title: 'Metal API', specification: 'metal/internal/api/swagger.json', server: 'https://metal.example.com:9000' },
]

function readSpecification(reference: ApiReference): string {
	const path = resolve(root, reference.specification)
	if (!existsSync(path)) {
		throw new Error(`${reference.title} specification is missing at ${reference.specification}. Run \`make -C metal openapi\` for Metal.`)
	}

	return readFileSync(path, 'utf8')
}

function renderPage(reference: ApiReference): string {
	// A read-only reference: no request client, AI agent, MCP, or telemetry.
	const configuration = {
		url: './openapi.json',
		servers: [{ url: reference.server }],
		hideClientButton: true,
		hideTestRequestButton: true,
		showDeveloperTools: 'never',
		documentDownloadType: 'json',
		telemetry: false,
		agent: { disabled: true },
		mcp: { disabled: true },
	}

	return `<!doctype html>
<html lang="en">
	<head>
		<title>${reference.title} · Atlas</title>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
	</head>
	<body>
		<div id="app"></div>
		<script src="../scalar.js"></script>
		<script>Scalar.createApiReference('#app', ${JSON.stringify(configuration)})</script>
	</body>
</html>
`
}

/** Return the published API reference files, keyed by path below the site root. */
function apiReferenceFiles(): Map<string, () => string | Buffer> {
	const files = new Map<string, () => string | Buffer>([['api/scalar.js', () => readFileSync(scalarScript)]])
	for (const reference of references) {
		files.set(`api/${reference.name}/index.html`, () => renderPage(reference))
		files.set(`api/${reference.name}/openapi.json`, () => readSpecification(reference))
	}

	return files
}

/** Write the API reference pages into a built site. */
export function writeApiReferences(outputDirectory: string): void {
	for (const [path, content] of apiReferenceFiles()) {
		const target = resolve(outputDirectory, path)
		mkdirSync(dirname(target), { recursive: true })
		writeFileSync(target, content())
	}
}

/** Serve the API reference pages from the development server. */
export function apiReferencePlugin(base: string): Plugin {
	return {
		name: 'atlas-api-reference',
		configureServer(server) {
			const files = apiReferenceFiles()
			server.middlewares.use((request, response, next) => {
				const path = (request.url ?? '').split('?')[0].replace(base, '').replace(/\/$/, '/index.html')
				const content = files.get(path)
				if (!content) return next()

				response.setHeader('Content-Type', path.endsWith('.html') ? 'text/html' : path.endsWith('.js') ? 'text/javascript' : 'application/json')
				response.end(content())
			})
		},
	}
}
