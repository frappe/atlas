// A viewport-aware page size. The dashboard never scrolls the page — lists
// paginate to fit — but a fixed rows-per-page can't fit both a tall monitor and a
// short laptop. This derives the row count from the live viewport height so the
// table fills the space it has and no more, on any screen size.
//
//   const perPage = usePageSize({ rowPx: 33, reserve: 300, min: 6, max: 22 });
//
// `rowPx` is the approximate rendered height of one row; `reserve` is the chrome
// above+below the rows (header, panel heading, column header, pager, footer).
import { ref, onMounted, onBeforeUnmount } from "vue";

export function usePageSize({ rowPx = 33, reserve = 300, min = 5, max = 25, step = 5 } = {}) {
	const perPage = ref(min);

	function measure() {
		const h = window.innerHeight || 800;
		const fits = Math.floor((h - reserve) / rowPx);
		// Snap DOWN to a round step (5, 10, 15, 20…) so the page size reads as a
		// round number ("10 of 24", not "11 of 24") and stays stable across small
		// viewport nudges. Never below `min`.
		const rounded = Math.floor(fits / step) * step;
		perPage.value = Math.max(min, Math.min(max, rounded || min));
	}

	onMounted(() => {
		measure();
		window.addEventListener("resize", measure, { passive: true });
	});
	onBeforeUnmount(() => window.removeEventListener("resize", measure));

	return perPage;
}
