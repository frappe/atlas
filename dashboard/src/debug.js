// Alignment debug: a switch that renders guide lines so you can eyeball whether
// side-by-side items line up. Mirrors theme.js — a reactive ref, seeded from the
// URL (?borders), persisted, and applied as a class on <html>. The actual lines
// live in style.css so the toggle is a class flip with no reflow.
//
// Three modes cycled by the header switch:
//   'off'      — nothing.
//   'borders'  — a thin OUTLINE on every element (its own box edges). Outlines
//                draw outside the box and take no layout space, so switching them
//                on shifts nothing — the alignment you inspect is the real one.
//   'extreme'  — everything 'borders' does, PLUS each element's top/left edge is
//                extended as a hairline across the WHOLE window, so any two edges
//                on the same column/row land on one continuous line when aligned.
import { ref, watch } from "vue";

const KEY = "atlas.debug.borders";
const MODES = ["off", "borders", "extreme"];

// URL wins over storage on load. ?borders / ?borders=1 → borders;
// ?borders=extreme (or =2 / =full) → extreme; ?borders=0 → off; else the
// persisted value; else off.
function initial() {
	const p = new URLSearchParams(window.location.search);
	if (p.has("borders")) {
		const v = (p.get("borders") || "").toLowerCase();
		if (v === "0" || v === "false" || v === "off") return "off";
		if (v === "extreme" || v === "full" || v === "2") return "extreme";
		return "borders";
	}
	const s = localStorage.getItem(KEY);
	return MODES.includes(s) ? s : "off";
}

export const borders = ref(initial());

function apply() {
	const root = document.documentElement;
	// extreme is a superset of borders — it draws the per-box outlines too.
	root.classList.toggle("debug-borders", borders.value !== "off");
	root.classList.toggle("debug-extreme", borders.value === "extreme");
}

watch(borders, (m) => {
	localStorage.setItem(KEY, m);
	apply();
});

apply();

// Cycle off → borders → extreme → off, for the header button.
export function toggleBorders() {
	const i = MODES.indexOf(borders.value);
	borders.value = MODES[(i + 1) % MODES.length];
}
