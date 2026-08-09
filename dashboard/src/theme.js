// Theme: three modes — 'system' (follow the OS), 'light', 'dark'. The choice is
// persisted so a refresh (the only update mechanism) keeps it. frappe-ui's
// tokens read data-theme on <html>; 'system' resolves to the OS at apply time
// and re-resolves live when the OS flips.
import { ref, watch } from "vue";

const KEY = "atlas.theme";
const media = window.matchMedia("(prefers-color-scheme: dark)");

function stored() {
	const v = localStorage.getItem(KEY);
	return v === "light" || v === "dark" || v === "system" ? v : "system";
}

export const mode = ref(stored());

function resolved(m) {
	if (m === "system") return media.matches ? "dark" : "light";
	return m;
}

function apply() {
	document.documentElement.setAttribute("data-theme", resolved(mode.value));
}

// Persist + re-apply on every change; re-resolve when the OS flips (only matters
// in 'system' mode, but re-applying in any mode is harmless).
watch(mode, (m) => {
	localStorage.setItem(KEY, m);
	apply();
});
media.addEventListener("change", apply);

apply();

// Flip light ↔ dark, for the header button. The FIRST click resolves the current
// on-screen theme (which may be 'system') to a concrete light/dark and toggles
// from there, so there's no dead press. 'system' remains the seeded default and
// the persisted value until the user first touches the toggle.
export function cycleMode() {
	mode.value = resolved(mode.value) === "dark" ? "light" : "dark";
}
