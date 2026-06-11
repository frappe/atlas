"""Image recipe registry — the code-defined catalogue of buildable images.

Atlas builds an image by provisioning a plain Ubuntu VM, running a committed
`build.sh` inside it over guest-SSH, then snapshotting the result (the
build-in-guest + snapshot pattern, spec/08-images.md, spec/12-proxy.md). The two
images this exists for — the golden bench image and the reverse-proxy image —
differ only in *which committed tree* gets uploaded, the build-VM sizing, a
post-build finalize step, and what to do with the snapshot. An `ImageRecipe`
captures exactly those differences; everything else (upload, run detached, record
a Task, fail loud) is the shared `atlas.atlas.image_builder.run_build` seam.

This is a Python registry, NOT a DocType: a recipe points entirely at committed
files (`bench/`, `proxy/`) and pinned sizes, and `finalize` is a callback — a
data row could only ever mirror it. Adding an image type is a small reviewable
code change beside the committed tree it bakes, the same discipline the two
`build.sh` files' pinned versions already follow (spec taste: few dependencies,
pinned versions are a deliberate update).
"""

import shlex
from collections.abc import Callable
from dataclasses import dataclass

import frappe

from atlas.atlas._ssh.transport import run_ssh


@dataclass(frozen=True)
class ImageRecipe:
	"""One buildable image. `name` is the registry key the operator picks.

	`source_directory` is a repo-root-relative tree (uploaded verbatim);
	`build_entrypoint` is the script inside it run over guest-SSH. The build VM is
	provisioned at `vcpus`/`memory_megabytes`/`disk_gigabytes`. `snapshot_title`
	is stamped on the produced snapshot. `exclude` drops top-level tree entries
	from the upload (the proxy's dev-only compose harness). `finalize`, if set, is
	a post-build guest step (the proxy writes its region + restarts its unit).
	`registers_as`, if set, names the Atlas Settings field a successful build
	wires the snapshot into. `is_proxy` marks the produced VMs as proxies (so the
	build is region-scoped and the build VM carries `is_proxy`/`region`)."""

	name: str
	title: str
	source_directory: str
	build_entrypoint: str
	remote_directory: str
	disk_gigabytes: int
	memory_megabytes: int
	vcpus: int
	snapshot_title: str
	task_script: str
	exclude: tuple[str, ...] = ()
	finalize: Callable | None = None
	registers_as: str | None = None
	is_proxy: bool = False
	# The in-guest script (inside source_directory, like build_entrypoint) that
	# arms a WARM bake: bring the production stack up, pre-warm it with real
	# HTTP, and install the identity freshen unit — run right before the paused
	# memory+disk capture. Empty = the recipe can only bake cold.
	warm_entrypoint: str = ""

	@property
	def build_log_path(self) -> str:
		"""Where the detached build tees its log on the guest (run_detached)."""
		return f"{self.remote_directory}/build.log"

	@property
	def build_done_path(self) -> str:
		"""Where the detached build stamps its exit code on the guest."""
		return f"{self.remote_directory}/build.done"

	@property
	def remote_entrypoint(self) -> str:
		return f"{self.remote_directory}/{self.build_entrypoint}"


def _finalize_proxy(virtual_machine, connection, key_path) -> tuple[str, str, int]:
	"""Post-build proxy step: write the real region (build.sh leaves it empty) and
	(re)start the unit so init_by_lua picks the region up.

	We deliberately do NOT repoint the cert symlink here: build.sh aims the flat
	certs/{fullchain,privkey}.pem at the `_placeholder` cert (which exists), so
	nginx starts with a valid cert. Repointing to certs/<region>/ happens in
	push_cert, AFTER the real cert is written there — repointing now would dangle
	the symlink and nginx would fail to load the cert at start. `systemctl restart`
	is a no-op-to-start on a guest with no running unit yet, a clean restart on a
	rebuild. Returns (stdout, stderr, exit_code) so run_build records it like the
	build itself."""
	# Local import: proxy.py imports image_builder (for build_proxy → run_build),
	# which imports this module — so importing proxy at module scope would cycle.
	# REGION_FILE is a plain constant; pull it in only when the finalize runs.
	from atlas.atlas.proxy import REGION_FILE

	region = virtual_machine.region
	command = (
		f"printf '%s\\n' {shlex.quote(region)} > {shlex.quote(REGION_FILE)} && "
		"systemctl restart atlas-proxy.service"
	)
	return run_ssh(connection, key_path, command, timeout_seconds=120)


# The build VM clones Frappe + builds a uv venv + Node deps; 4 GB is too tight, so
# the bench build VM (and therefore the snapshot, and clones from it) gets a
# roomier disk and 2 GB RAM. These were GOLDEN_DISK_GB / GOLDEN_MEMORY_MB in the
# e2e module; they live with the recipe now (spec/14 "~2 GB/site" host-sizing).
_BENCH_DISK_GB = 12
_BENCH_MEMORY_MB = 2048


RECIPES: dict[str, "ImageRecipe"] = {
	"bench": ImageRecipe(
		name="bench",
		title="Golden bench image",
		source_directory="bench",
		build_entrypoint="build.sh",
		remote_directory="/tmp/atlas-bench-build",
		disk_gigabytes=_BENCH_DISK_GB,
		memory_megabytes=_BENCH_MEMORY_MB,
		vcpus=2,
		snapshot_title="golden-bench",
		task_script="bench-build",
		registers_as="default_bench_snapshot",
		warm_entrypoint="warm.sh",
	),
	"proxy": ImageRecipe(
		name="proxy",
		title="Reverse proxy image",
		source_directory="proxy",
		build_entrypoint="build.sh",
		remote_directory="/tmp/atlas-proxy-build",
		disk_gigabytes=10,
		memory_megabytes=1024,
		vcpus=2,
		snapshot_title="proxy-image",
		task_script="proxy-build",
		# The compose test harness under proxy/test/ is dev-only; the guest build
		# needs only build.sh + conf/lua/html/guest.
		exclude=("test",),
		finalize=_finalize_proxy,
		is_proxy=True,
	),
}


def get_recipe(name: str) -> "ImageRecipe":
	if name not in RECIPES:
		frappe.throw(f"Unknown image recipe {name!r}; known: {sorted(RECIPES)}")
	return RECIPES[name]


def recipe_names() -> list[str]:
	"""The recipe keys, for the Image Build `recipe` Select options."""
	return list(RECIPES)
