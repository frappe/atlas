// Windowed pagination that fits the viewport. Every long list in the dashboard
// needs the same three things: a viewport-tracking page size (usePageSize), a
// current-page slice, and two guards — reset to page 1 when the source changes,
// step back a page when the viewport shrinks past the end. This folds all of it
// into one call so the five list components stop repeating it.
//
//   const { page, pageRows, perPage, setPage } = usePagedRows(rowsRef, { rowPx, reserve });
//
// `rowsRef` is a ref/computed of the full array; the page-size opts match
// usePageSize. Pass `enabled: someRef` (default always-on) to make paging opt-in
// per render — AlertsList shows an unpaginated list inside the header modal.
import { ref, computed, watch, unref } from "vue";
import { usePageSize } from "./usePageSize.js";

export function usePagedRows(rowsRef, opts = {}) {
	const { enabled = true, ...sizeOpts } = opts;
	const perPage = usePageSize(sizeOpts);
	const page = ref(1);

	const all = () => unref(rowsRef) || [];
	const pageRows = computed(() => {
		if (!unref(enabled)) return all();
		const start = (page.value - 1) * perPage.value;
		return all().slice(start, start + perPage.value);
	});

	// A new source resets to the first page.
	watch(rowsRef, () => (page.value = 1));
	// A shorter viewport (fewer rows/page) can leave `page` past the end — step back.
	watch(perPage, () => {
		const maxPage = Math.max(1, Math.ceil(all().length / perPage.value));
		if (page.value > maxPage) page.value = maxPage;
	});

	const setPage = (p) => (page.value = p);
	return { page, pageRows, perPage, setPage };
}
