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
			return json.loads(line[len(RESULT_MARKER) :])
	raise ValueError(f"no {RESULT_MARKER} line in task output")


def parse_optional_result(stdout: str) -> dict | None:
	"""`parse_result`'s tolerant twin: the decoded payload, or None when the output
	carries no marker line.

	For the caller whose verb may run over a transport that states no typed result
	at all. Boat's `Operation` carries `output`, `error` and `exit_code` and nothing
	else (`api/openapi.yaml`), so a verb driven through Boat lands a Task whose
	stdout is the daemon's trace with no `ATLAS_RESULT=` line in it — and a caller
	that has to record what the verb DID cannot make that conditional on learning a
	detail its transport never carried. `VirtualMachine.sleep` is the one: the VM is
	parked either way, so the row is `Sleeping` either way (spec/32).

	Everything else keeps `parse_result`. A task that declares a typed result and
	produces none is a truncated run, and that must still surface loudly."""
	try:
		return parse_result(stdout)
	except ValueError:
		return None


def result_line(result: dict) -> str:
	"""One `ATLAS_RESULT=` line for a result that reached the controller some way
	other than a task script's stdout — today, a Boat operation's typed result
	folded onto the Task row it filled (`boat_client._task_stdout`).

	Written here because this module owns the marker's shape, and on ONE line
	because `parse_result` reads lines."""
	return f"{RESULT_MARKER}{json.dumps(result)}\n"
