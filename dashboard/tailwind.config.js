import frappeUIPreset from "frappe-ui/tailwind";

// Use frappe-ui's design tokens (the --ink-gray / --surface / --outline scale
// and Inter) so the dashboard reads as Frappe without pulling any colour in.
// We deliberately touch only the neutral tokens in the app CSS.
export default {
	presets: [frappeUIPreset],
	content: [
		"./index.html",
		"./src/**/*.{vue,js,ts}",
		"./node_modules/frappe-ui/src/**/*.{vue,js,ts}",
	],
	theme: {
		extend: {
			// The one non-neutral thing we keep from the old token layer: the SF Mono
			// subset (shipped as a woff2 in src/fonts, @font-face'd in style.css) so
			// UUIDs / addresses / sizes line up. `font-mono` now leads with it; the
			// rest of the stack is the system mono fallback. Everything else — colour,
			// sizes, radii — comes from frappe-ui's preset + Tailwind utilities.
			fontFamily: {
				mono: ['"SFMono"', "ui-monospace", "Menlo", "monospace"],
			},
			// frappe-ui's preset exposes the ink scale as text/fill only and the
			// outline scale as border/ring only — never as `background-color`. But the
			// dashboard's one gauge (Meter.vue) paints an *ink-coloured* fill on a
			// hairline track, which is a legitimate background use the preset doesn't
			// cover. Map the two neutral scales into `backgroundColor` (via the same
			// --ink-gray / --outline-gray tokens, so they still flip on [data-theme])
			// so `bg-ink-gray-N` / `bg-outline-gray-1` resolve instead of emitting
			// nothing. Scoped to the greys we actually use as fills.
			backgroundColor: {
				"ink-gray-5": "var(--ink-gray-5)",
				"ink-gray-6": "var(--ink-gray-6)",
				"ink-gray-7": "var(--ink-gray-7)",
				"ink-gray-8": "var(--ink-gray-8)",
				"ink-gray-9": "var(--ink-gray-9)",
				"outline-gray-1": "var(--outline-gray-1)",
			},
		},
	},
};
