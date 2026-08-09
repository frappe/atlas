import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// In dev there is no host and no Python backend, so serve checked-in fixtures at
// the same /api/state path the real backend answers. Fixtures are listed in
// mock/sources.json ({id,label,file}); a `?src=<id>` selects one (default: the
// first), so the header can flip between the mock contract and a real host
// capture. Read fresh on every request so editing a fixture live-reloads the
// data with a browser refresh — the whole app is refresh-to-update.
//
// This is dev-only. The shipped Python backend (backend/server.py) answers a
// single live /api/state off /var/lib/atlas and knows nothing about sources.
function mockApi() {
	const sources = () => JSON.parse(readFileSync(resolve("mock/sources.json"), "utf-8"));
	return {
		name: "atlas-mock-api",
		configureServer(server) {
			// The list of available fixtures — the header populates its switcher here.
			server.middlewares.use("/api/state/sources", (req, res) => {
				res.setHeader("Content-Type", "application/json");
				res.setHeader("Cache-Control", "no-store");
				res.end(JSON.stringify(sources().map(({ id, label }) => ({ id, label }))));
			});
			server.middlewares.use("/api/state", (req, res) => {
				const list = sources();
				const id = new URL(req.url, "http://x").searchParams.get("src");
				const pick = list.find((s) => s.id === id) || list[0];
				res.setHeader("Content-Type", "application/json");
				res.setHeader("Cache-Control", "no-store");
				try {
					res.end(readFileSync(resolve("mock", pick.file), "utf-8"));
				} catch (e) {
					res.statusCode = 404;
					res.end(JSON.stringify({ error: `fixture ${pick.file} not found` }));
				}
			});
		},
	};
}

export default defineConfig({
	plugins: [vue(), mockApi()],
	// Ship as plain static files copied onto the host next to the backend; keep
	// asset URLs relative so it works regardless of where it is mounted.
	base: "./",
	build: {
		outDir: "dist",
		emptyOutDir: true,
	},
});
