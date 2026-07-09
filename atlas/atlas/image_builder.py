"""Image builder — the shared seam that builds an image INSIDE a guest over SSH.

`run_build(vm, recipe)` is the de-duplicated core of what `bench_image.build_bench`
and `proxy.build_proxy` both used to do verbatim: upload a committed source tree
into a freshly-provisioned guest, run its `build.sh` DETACHED (so a mid-build SSH
reset doesn't kill the long compile/bake), run the recipe's finalize step, and
record one Task row for the operator's audit trail — failing loud on a non-zero
exit. The two `build_*` functions are now thin wrappers that hand it a recipe.

The full provision→build→snapshot→register lifecycle around this seam lives in the
`Image Build` DocType (spec/15-image-builder.md); this module owns only the
upload+build+finalize+audit half. It SSHes *into the guest* (`connection_for_guest`,
the second SSH target type, spec/04), not onto a Server — the same path the proxy
control plane uses.
"""

import os
import shlex
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import frappe

from atlas.atlas._ssh._quote import substitute
from atlas.atlas._ssh.transport import forget_host, run_detached, run_scp, run_ssh, ssh_key_file
from atlas.atlas.image_recipes import ImageRecipe
from atlas.atlas.proxy import _record_guest_task, _remote_parent
from atlas.atlas.ssh import connection_for_guest

# The committed bench.toml the bench recipes render per-version. Its `[bench] python`
# and the `frappe` app's `[[apps]] branch` are the only lines that vary between
# v15/v16/nightly; everything else (production shape, MariaDB, Redis, ZFS) is the
# proven invariant. A recipe whose source tree has no bench.toml (proxy) renders
# nothing — _render_bench_toml returns None and the verbatim upload stands.
BENCH_TOML_NAME = "bench.toml"


def _source_directory(recipe: ImageRecipe) -> Path:
	"""The committed tree the recipe bakes, e.g. `<repo>/bench` or `<repo>/proxy`.
	The `..` resolves the app symlink to the repo root, where these trees sit
	beside `scripts/` (the same idiom as scripts_catalog.scripts_directory())."""
	return Path(frappe.get_app_path("atlas", "..")).resolve() / recipe.source_directory


def tree_uploads(recipe: ImageRecipe) -> list[tuple[Path, str]]:
	"""Every committed file under the recipe's source tree, mapped to its remote
	path under `recipe.remote_directory`, preserving the relative layout so
	`build.sh` finds its siblings (bench.toml, or conf/lua/html/guest) beside
	itself — it reads from its own directory. Top-level entries in `recipe.exclude`
	(the proxy's dev-only `test/` harness) and any `__pycache__` are skipped."""
	source = _source_directory(recipe)
	uploads: list[tuple[Path, str]] = []
	for entry in sorted(source.rglob("*")):
		if not entry.is_file():
			continue
		relative = entry.relative_to(source)
		if relative.parts[0] in recipe.exclude or "__pycache__" in relative.parts:
			continue
		uploads.append((entry, f"{recipe.remote_directory}/{relative.as_posix()}"))
	# Fail loud if a declared entrypoint isn't in the tree. is_file() above silently
	# skips a missing file, but the build/warm steps still invoke recipe.<entrypoint>
	# by name — a stale app checkout (missing warm.sh) then dies deep in the guest with
	# a cryptic "No such file or directory". Catch it here, at the source of truth.
	staged = {remote for _, remote in uploads}
	for entrypoint in (recipe.build_entrypoint, recipe.warm_entrypoint):
		if entrypoint and f"{recipe.remote_directory}/{entrypoint}" not in staged:
			frappe.throw(
				f"Recipe {recipe.name} declares entrypoint {entrypoint!r} but it is not "
				f"in source tree {source} (stale app checkout?)"
			)
	return uploads


def _render_bench_toml(recipe: ImageRecipe) -> str | None:
	"""Render the committed `bench/bench.toml` for this recipe's version pins, or
	None if the recipe pins nothing (proxy, or a future bench recipe that wants the
	committed default verbatim).

	Stdlib only — line-targeted substitution, no Jinja2 (a template format would
	clash with TOML's own `{ }` and add a dependency for two lines). We rewrite:

	  * `[bench] python = "<X>"`     ← recipe.python_version
	  * the `frappe` app's `[[apps]] branch = "<X>"`   ← recipe.frappe_branch

	The branch edit is SECTION-AWARE: bench.toml can carry more than one `[[apps]]`
	block (each with its own `branch`), so we only touch the `branch` line inside the
	block whose `name = "frappe"` — never a sibling app's branch. ERPNext's branch is
	NOT in bench.toml (build.sh clones ERPNext with `get-app --branch`), so it rides
	the ERPNEXT_BRANCH env override instead (see _build_env). Fails loud if a targeted
	line is missing — a silent no-op would bake the wrong version."""
	if not (recipe.frappe_branch or recipe.python_version):
		return None
	toml_path = _source_directory(recipe) / BENCH_TOML_NAME
	if not toml_path.exists():
		frappe.throw(f"Recipe {recipe.name} pins a version but {toml_path} has no bench.toml to render")
	lines = toml_path.read_text().splitlines(keepends=True)

	current_table = ""  # the most recent [table] / [[array.table]] header seen
	in_frappe_app = False  # are we inside the `frappe` [[apps]] block?
	python_done = branch_done = False
	out: list[str] = []
	for line in lines:
		stripped = line.strip()
		if stripped.startswith("["):
			current_table = stripped
			# A new [[apps]] block resets the frappe flag; we set it true only once we
			# see this block's `name = "frappe"` line below.
			in_frappe_app = False
		elif current_table == "[[apps]]" and stripped.startswith("name"):
			in_frappe_app = '"frappe"' in stripped or "'frappe'" in stripped
		if recipe.python_version and current_table == "[bench]" and _is_key(stripped, "python"):
			out.append(_set_string_key(line, "python", recipe.python_version))
			python_done = True
			continue
		if recipe.frappe_branch and in_frappe_app and _is_key(stripped, "branch"):
			out.append(_set_string_key(line, "branch", recipe.frappe_branch))
			branch_done = True
			continue
		out.append(line)

	if recipe.python_version and not python_done:
		frappe.throw(f"bench.toml has no [bench].python line to pin for recipe {recipe.name}")
	if recipe.frappe_branch and not branch_done:
		frappe.throw(f"bench.toml has no frappe [[apps]].branch line to pin for recipe {recipe.name}")
	return "".join(out)


def _is_key(stripped_line: str, key: str) -> bool:
	"""True if a stripped TOML line assigns `key` (`key = …`), not a comment or a
	different key that happens to start with the same letters (`python_path`)."""
	if stripped_line.startswith("#"):
		return False
	head = stripped_line.split("=", 1)[0].strip() if "=" in stripped_line else ""
	return head == key


def _set_string_key(line: str, key: str, value: str) -> str:
	"""Rewrite a `key = "old"` line to `key = "<value>"`, preserving the leading
	indentation and the trailing newline (an inline `# comment` is dropped — none of
	the targeted lines carry one)."""
	indent = line[: len(line) - len(line.lstrip())]
	newline = "\n" if line.endswith("\n") else ""
	return f'{indent}{key} = "{value}"{newline}'


def _build_env(recipe: ImageRecipe) -> dict[str, str]:
	"""The env overrides build.sh reads (it defaults each when unset, so a direct
	`build.sh` run with no env stays reproducible). bench-cli ref + ERPNext branch
	are env, not bench.toml: the ref is install.sh's checkout target and the ERPNext
	branch is a `get-app --branch` arg, neither of which lives in bench.toml."""
	env: dict[str, str] = {}
	if recipe.bench_cli_ref:
		env["BENCH_CLI_REF"] = recipe.bench_cli_ref
	if recipe.erpnext_branch:
		env["ERPNEXT_BRANCH"] = recipe.erpnext_branch
	return env


def _build_command(recipe: ImageRecipe) -> str:
	"""The detached build command: export the version env, make the entrypoint
	executable, then run it with the bake MODE as its positional arg. The whole
	remote command string is caller-controlled (run_detached just wraps it in
	setsid+nohup), so the version pins ride here — build.sh needs no new arg-parsing
	beyond MODE, which it already has."""
	env = _build_env(recipe)
	# Variable-length env exports — a loop, so quote each value directly (the {} form
	# is for fixed templates). The entrypoint is a caller-fixed path; only the bake
	# MODE is data, so it rides a {} hole.
	prefix = "".join(substitute("export {}={} && ", (k, v)) for k, v in env.items())
	entry = recipe.remote_entrypoint
	return prefix + substitute(f"chmod +x {entry} && {entry} {{}}", (recipe.effective_build_mode,))


# How much streamed live output to keep on the Task row while it runs. A
# `bench init` log runs to hundreds of KB; the full text still lands in `stdout`
# at completion, so the live buffer only needs to be the recent tail an operator
# is actually watching. Kept small to keep each per-poll row write cheap.
LIVE_OUTPUT_BUFFER_BYTES = 16 * 1024


class GuestTaskStream:
	"""A `proxy-build`/`bench-build` Task that exists as `Running` from the start
	of the detached build and streams the guest log onto itself as it runs (spec/22).

	The non-streaming path (`_record_guest_task`) inserts the row only on
	completion, so the operator sees nothing for the 10-20 min the build takes.
	This pre-inserts the row, exposes `on_log` as the `run_detached` sink — each
	chunk is appended to `live_output` (bounded tail), the last line surfaced as
	`progress_line`, committed, and pushed over the `task_log` realtime event — and
	`finalize` writes the terminal status + authoritative full stdout/stderr.

	The row name is identical in shape to `_record_guest_task`'s, so a streamed
	build appears in the same Task list as every other guest op; only its timing
	(visible immediately, tailing live) differs."""

	def __init__(self, virtual_machine: str, script: str, variables: dict) -> None:
		self.task = frappe.get_doc(
			{
				"doctype": "Task",
				"server": frappe.db.get_value("Virtual Machine", virtual_machine, "server"),
				"virtual_machine": virtual_machine,
				"script": script,
				"status": "Running",
				"triggered_by": frappe.session.user if frappe.session else "Administrator",
				"started": frappe.utils.now_datetime(),
			}
		)
		self.task.variables_dict = variables
		self.task.insert(ignore_permissions=True)
		# nosemgrep: frappe-manual-commit -- make the Running row visible before the long detached build begins (and so the streamed appends below have a committed row to update)
		frappe.db.commit()
		self._buffer = ""

	def on_log(self, chunk: str) -> None:
		"""run_detached sink: append a freshly-read log tail to the live view."""
		self._buffer = (self._buffer + chunk)[-LIVE_OUTPUT_BUFFER_BYTES:]
		last_line = next((line for line in reversed(self._buffer.splitlines()) if line.strip()), "")
		self.task.db_set("live_output", self._buffer, update_modified=False)
		self.task.db_set("progress_line", last_line[:140], update_modified=False)
		# nosemgrep: frappe-manual-commit -- persist each streamed chunk so the desk form's polling fallback (and a crash mid-build) sees progress cross-transaction
		frappe.db.commit()
		self.task.publish_log(live_output=self._buffer, progress_line=last_line[:140])

	def finalize(self, stdout: str, stderr: str, exit_code: int) -> str:
		"""Write the terminal status + the authoritative full output, superseding
		the streamed tail. Returns the Task name (for the audit-link callback)."""
		self.task.status = "Success" if exit_code == 0 else "Failure"
		self.task.stdout = stdout
		self.task.stderr = stderr
		self.task.exit_code = exit_code
		self.task.ended = frappe.utils.now_datetime()
		self.task.save(ignore_permissions=True)
		# nosemgrep: frappe-manual-commit -- persist the terminal Task outcome before run_build re-raises on a failed build
		frappe.db.commit()
		return self.task.name


def run_build(
	virtual_machine: str,
	recipe: ImageRecipe,
	on_task: Callable[[str], None] | None = None,
	stream: bool = False,
) -> None:
	"""Upload the recipe's committed tree into the guest, run its build entrypoint
	DETACHED, then run the recipe's finalize hook. Records one Task row (named by
	`recipe.task_script`) and throws on any non-zero exit.

	`on_task`, if given, is called with the build Task's name so a caller (the
	Image Build controller) can link it for its audit trail. In the non-stream path
	the row only exists on completion, so this fires once at the end (still BEFORE
	the throw, so a failed build is linked too). In the STREAM path the Running row
	exists up front, so `on_task` fires immediately after insert — the Image Build
	form can then surface the live, tailing Task while the bake runs, not only after
	it finishes.

	`stream` (spec/22 sample): when True the build Task is created `Running` up
	front and the guest build log is streamed onto it live via a `GuestTaskStream`
	sink, instead of the row only appearing on completion. The default (False) is
	the original behavior byte-for-byte — `_record_guest_task` inserts the finished
	row and `run_detached` makes no extra SSH calls.

	Idempotent: the committed `build.sh` scripts are idempotent (spec taste #16,
	retry = re-run), so this doubles as the re-bake verb."""
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	connection = connection_for_guest(vm)
	# Freshly-provisioned VM, possibly on a recycled IP whose old host key we
	# pinned. This path goes straight to run_scp/run_ssh (no wait_for_ssh), so
	# accept-new never re-pins a CHANGED key — drop the stale entry first or the
	# first scp hard-fails "REMOTE HOST IDENTIFICATION HAS CHANGED"
	# (real-provision-traps #1).
	forget_host(connection.host)
	sink = GuestTaskStream(virtual_machine, recipe.task_script, {"recipe": recipe.name}) if stream else None
	if sink and on_task:
		# Link the Running row NOW so the Image Build form shows the live build Task
		# during the bake, not only at completion.
		on_task(sink.task.name)
	# ExitStack owns the rendered-bench.toml temp file: it must stay on disk across
	# the whole _stage_tree (which scp's it), and be unlinked on the way out whether
	# the build succeeds or throws — hence the stack, not a bare `with tempfile`.
	with ExitStack() as stack:
		uploads = _uploads_with_rendered_toml(recipe, stack)
		key_path = stack.enter_context(ssh_key_file(connection.ssh_private_key))
		_stage_tree(connection, key_path, uploads)
		# Run the build (long: apt + clone + uv for bench; nginx + luajit compile
		# for proxy) DETACHED, so a connection reset mid-build doesn't SIGHUP it.
		# The shared run_detached helper owns the setsid+nohup + marker-poll
		# mechanics; we hand it the entrypoint (with the version env + bake mode)
		# and its own log/done paths.
		stdout, stderr, code = run_detached(
			connection,
			key_path,
			_build_command(recipe),
			log_path=recipe.build_log_path,
			done_path=recipe.build_done_path,
			on_log=sink.on_log if sink else None,
		)
		if code == 0 and recipe.finalize:
			# Fast follow-up after a successful build (no detach needed). Its
			# stdout/stderr/code become the recorded result, so a finalize failure
			# is a build failure.
			stdout, stderr, code = recipe.finalize(vm, connection, key_path)
	if sink:
		# on_task already fired at insert (above) for the stream path; finalize just
		# writes the terminal status + authoritative output onto the same row.
		sink.finalize(stdout, stderr, code)
	else:
		task_name = _record_guest_task(
			virtual_machine, recipe.task_script, {"recipe": recipe.name}, stdout, stderr, code
		)
		if on_task:
			on_task(task_name)
	if code != 0:
		frappe.throw(f"{recipe.title} build on {virtual_machine} failed (exit {code}): {stderr[-500:]}")


def stage_recipe_tree(recipe: ImageRecipe, connection, key_path) -> None:
	"""Re-stage the recipe's committed tree into the guest under `remote_directory`,
	rendered bench.toml and all. run_build stages it once before the build, but the
	tree lives under /tmp (tmpfs) — a bake that reboots the guest between build and a
	later in-guest step (the fat-boot resize, then the warm entrypoint) loses it. The
	warm path calls this to put warm.sh + its siblings back before invoking them; a
	fresh stage is idempotent (scp overwrites) and cheap (the tree is small)."""
	with ExitStack() as stack:
		uploads = _uploads_with_rendered_toml(recipe, stack)
		_stage_tree(connection, key_path, uploads)


def _uploads_with_rendered_toml(recipe: ImageRecipe, stack: ExitStack) -> list[tuple[Path, str]]:
	"""The recipe's tree uploads, with the committed bench.toml swapped for a rendered
	temp file when the recipe pins a version. The temp file is registered with `stack`
	so it is unlinked when run_build's ExitStack closes (after staging, success or
	throw). A recipe that renders nothing (proxy) uploads its tree verbatim.

	Fails loud if the recipe pins a version but its tree carries no bench.toml entry
	to swap — a silent skip would scp the wrong (unpinned) config and bake the wrong
	version, the exact footgun this guard prevents."""
	uploads = tree_uploads(recipe)
	rendered = _render_bench_toml(recipe)
	if rendered is None:
		return uploads
	remote_toml = f"{recipe.remote_directory}/{BENCH_TOML_NAME}"
	index = next((i for i, (_, remote) in enumerate(uploads) if remote == remote_toml), None)
	if index is None:
		frappe.throw(
			f"Recipe {recipe.name} renders a versioned bench.toml but its tree has no "
			f"{BENCH_TOML_NAME} upload to swap (expected remote {remote_toml})"
		)
	handle = tempfile.NamedTemporaryFile("w", suffix=f"-{BENCH_TOML_NAME}", delete=False)
	handle.write(rendered)
	handle.close()
	stack.callback(lambda: os.unlink(handle.name))
	uploads[index] = (Path(handle.name), remote_toml)
	return uploads


def _stage_tree(connection, key_path, uploads: list[tuple[Path, str]]) -> None:
	"""mkdir -p every remote parent dir in one SSH call, then scp every file.
	Staging the whole tree under one dir is what lets `build.sh` find its siblings
	(it reads from its own directory)."""
	remote_dirs = sorted({_remote_parent(remote) for _, remote in uploads})
	run_ssh(
		connection,
		key_path,
		"mkdir -p " + " ".join(shlex.quote(directory) for directory in remote_dirs),
		timeout_seconds=60,
	)
	for local, remote in uploads:
		run_scp(connection, key_path, str(local), remote, timeout_seconds=300)
