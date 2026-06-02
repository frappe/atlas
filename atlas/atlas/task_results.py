"""Controller-side parser for a Task's typed result line.

Python tasks emit one `ATLAS_RESULT=<json>` line (see
scripts/lib/atlas/_task.py::TaskResult.emit). This is the controller half: pull
that line out of a Task's stdout and return the decoded dict. It replaces the
ad-hoc per-script stdout scraping the controllers used to do
(`_parse_size_bytes`, the bootstrap-json tail-line read).

The marker string is duplicated here intentionally: the emitting half lives in
the remote, stdlib-only `atlas` package (staged onto the host, never importable
by the Frappe app), so the two sides cannot share a module. The contract is one
constant; keep them in sync.
"""

import json

RESULT_MARKER = "ATLAS_RESULT="


def parse_result(stdout: str) -> dict:
	"""Return the decoded `ATLAS_RESULT=` payload from a task's stdout.

	Takes the LAST marker line (a re-run or retry appends; the final one wins).
	Raises ValueError if no marker is present — unlike the old `_parse_size_bytes`
	(which silently returned 0), a task that declares a typed result must produce
	one, so a truncated/failed run surfaces loudly."""
	for line in reversed((stdout or "").splitlines()):
		if line.startswith(RESULT_MARKER):
			return json.loads(line[len(RESULT_MARKER):])
	raise ValueError(f"no {RESULT_MARKER} line in task output")
