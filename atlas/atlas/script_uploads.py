"""Per-script sidecar uploads.

Every Python task imports the shared `atlas` package (lvm, paths, rootfs, _run,
_task, …). That package is DURABLE host state: `Server.bootstrap()` places it
once at `/var/lib/atlas/bin/atlas/` (see server.py). Tasks `import atlas` from
there — the runner sets `PYTHONPATH=/var/lib/atlas/bin` on the remote command
(see `_ssh/runner.py::_remote_command()`), so NO package files are re-staged per
Task. This trades per-Task package freshness for ~9 fewer SSH round-trips per
Task: a controller change to a lib module reaches a host on the next `bootstrap`,
the single refresh point (the same contract the systemd hooks already follow —
vm-network-up.py et al. run the durable copy too). See spec/04-tasks.md.

A few scripts still need extra sidecars (sync-image bakes the guest network unit
into the image it builds); those are staged per-Task via SCRIPT_SIDECARS.

Consumed by `_ssh/runner.py::_run_remote_script()` before each invocation.
Tuples are (local_relative_to_repo_root, remote_absolute).
"""

# Extra per-script sidecars beyond the durable package. sync-image bakes the
# guest atlas-network.service into the ext4 it builds, so it needs that file
# staged into the /tmp/atlas staging dir for that one Task.
SCRIPT_SIDECARS: dict[str, list[tuple[str, str]]] = {
	"sync-image": [
		("scripts/guest/atlas-network.service", "/tmp/atlas/atlas-network.service"),
	],
}


def files_to_upload(script: str) -> list[tuple[str, str]]:
	"""Sidecar files to stage before the verb `script` runs. The shared `atlas`
	package is NOT staged — it lives durably at /var/lib/atlas/bin from bootstrap
	and is reached as the pip-installed `atlas` console script (see module
	docstring). Only per-verb sidecars (and only for the few verbs that declare
	them) are uploaded."""
	return SCRIPT_SIDECARS.get(script, [])
