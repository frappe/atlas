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
	build runs the proxy finalize and the build VM carries `is_proxy`). The region
	the proxy serves is read from `Atlas Settings.region` at finalize time."""

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
	is_sshpiper: bool = False
	# The in-guest script (inside source_directory, like build_entrypoint) that
	# arms a WARM bake: bring the production stack up, pre-warm it with real
	# HTTP, and install the identity freshen unit — run right before the paused
	# memory+disk capture. Empty = the recipe can only bake cold.
	warm_entrypoint: str = ""
	# --- Per-version pins (bench recipes only; empty/None on proxy) ---
	# One committed `bench/` tree bakes any Frappe version: these pins are rendered
	# into bench.toml (frappe_branch → [[apps]].branch, python_version → [bench].python)
	# and injected as build.sh env overrides (bench_cli_ref → BENCH_CLI_REF,
	# erpnext_branch → ERPNEXT_BRANCH) by image_builder, so build.sh + bench.toml stay
	# the proven recipe and the *only* thing that varies between v15/v16/nightly is
	# this data. Empty means "use the committed default" (build.sh's own pin / the
	# bench.toml as-committed), which is exactly the proxy recipe's situation.
	frappe_branch: str = ""
	erpnext_branch: str = ""
	bench_cli_ref: str = ""
	python_version: str = ""
	# What the bake produces and a clone's FQDN maps to on first boot (spec/08):
	#   site  — a baked `site.local` renamed to the per-VM FQDN (the FQDN serves a site)
	#   admin — bench + admin app only, no site (the FQDN serves the admin console)
	# Threaded build VM → snapshot → clone → deploy-site.py `--mode`. Empty (proxy)
	# is treated as the default `site` everywhere it is read.
	build_mode: str = ""
	# The base-image name a promoted golden defaults to. For the three customer
	# variants this is the series image name (`bench-v15` / `bench-v16` /
	# `bench-nightly`). Empty falls back to `<recipe>-<build name>` (Image
	# Build.promote's old default).
	promote_image_name: str = ""

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

	@property
	def effective_build_mode(self) -> str:
		"""The bake mode build.sh/deploy-site.py understand: site or admin. A recipe
		that leaves `build_mode` empty (proxy) is treated as site — the harmless
		default (the proxy never threads a mode; only bench recipes do)."""
		return self.build_mode or "site"


def _finalize_sshpiper(virtual_machine, connection, key_path) -> tuple[str, str, int]:
	"""Move the baked gateway's management sshd to 222 after build polling ends."""
	return run_ssh(
		connection,
		key_path,
		"sshd -t && "
		"(systemctl disable --now ssh.socket >/dev/null 2>&1 || true) && "
		"systemctl mask ssh.socket >/dev/null && "
		"systemctl enable --now ssh.service >/dev/null && "
		"ss -ltnH 'sport = :222' | grep -q LISTEN",
		timeout_seconds=60,
	)


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
	from atlas.atlas.placement import atlas_region
	from atlas.atlas.proxy import REGION_FILE

	region = atlas_region()
	command = (
		f"printf '%s\\n' {shlex.quote(region)} > {shlex.quote(REGION_FILE)} && "
		"systemctl restart nginx.service"
	)
	return run_ssh(connection, key_path, command, timeout_seconds=120)


# The build VM clones Frappe + builds a uv venv + Node deps; 4 GB is too tight, so
# the bench build VM (and therefore the snapshot, and clones from it) gets a
# roomier disk and 2 GB RAM. These were GOLDEN_DISK_GB / GOLDEN_MEMORY_MB in the
# e2e module; they live with the recipe now (spec/14 "~2 GB/site" host-sizing).
#
# Sizing is dominated by the ZFS file vdev: bench.toml's [volume.image] preallocates
# a 15 GB image (`bench-pool.img`) on ROOT, so root must hold that 16 GB file PLUS
# the OS (~3 GB) PLUS the transient yarn cache + node_modules extraction `bench init`
# does on root (~4-5 GB) — the bench code/site itself lands on the ZFS dataset, but
# yarn's cache (~/.cache) does not. 20 GB proved too small once the vdev grew 7→15 GB:
# `bench init` died "ENOSPC … Extracting tar content" at the [9/15] yarn step with
# root 100% full (16 GB vdev left only ~4 GB). 28 GB restores ~9 GB of headroom above
# the vdev + OS for the node-deps build (and for MariaDB's /tmp during the ERPNext
# schema build). Keep this in step with bench.toml's [volume.image] size.
_BENCH_DISK_GB = 28
_BENCH_MEMORY_MB = 2048

# The proven bench-cli commit (main @ 2026-06-25, incl. the two-path install.sh,
# `bench rename-site`, and the IPv6-listeners commit dd14ad4) every bench variant
# builds with. bench-cli is the build *tool*, not the
# framework: it reads the Frappe branch + Python version from bench.toml and natively
# knows `version-15`/`version-16`/`develop` (core/app.py), so ONE ref bakes all three
# variants — the version lives in the per-recipe pins below, not in the tool. Pinned
# (not `main`) so the golden is reproducible; a variant can override it if a future
# Frappe release needs a newer bench-cli. Kept in lockstep with bench/build.sh's
# BENCH_CLI_REF default (the value a direct `build.sh` run uses with no env override).
_BENCH_CLI_REF = (
	"03a4272068f78a402d407c4ae9b071be5e00a14b"  # main @ 2026-06-25 (two-path install.sh + rename-site)
)


def _bench_variant(
	name: str,
	title: str,
	*,
	frappe_branch: str,
	erpnext_branch: str,
	python_version: str,
	build_mode: str = "site",
	registers_as: str | None = None,
	warm_entrypoint: str = "",
) -> ImageRecipe:
	"""A versioned golden bench recipe. The three customer variants (v15/v16/nightly)
	differ ONLY in their Frappe/ERPNext branch + Python pins, the bake mode, and their
	promote target image name (= the series name). Everything else — the committed
	`bench/` tree, the build-VM sizing, the bench-cli ref, the snapshot/task naming
	scheme — is shared. `promote_image_name` defaults to the recipe name
	(`bench-v15` etc.)."""
	return ImageRecipe(
		name=name,
		title=title,
		source_directory="bench",
		build_entrypoint="build.sh",
		remote_directory="/tmp/atlas-bench-build",
		disk_gigabytes=_BENCH_DISK_GB,
		memory_megabytes=_BENCH_MEMORY_MB,
		vcpus=2,
		snapshot_title=title,
		task_script="bench-build",
		frappe_branch=frappe_branch,
		erpnext_branch=erpnext_branch,
		bench_cli_ref=_BENCH_CLI_REF,
		python_version=python_version,
		build_mode=build_mode,
		registers_as=registers_as,
		warm_entrypoint=warm_entrypoint,
		promote_image_name=name,
	)


RECIPES: dict[str, "ImageRecipe"] = {
	# --- The three customer-facing golden bench variants. Baked per Frappe/Bench
	# release; promoted to a base image named exactly `bench-v<NN>` / `bench-nightly`
	# so customers pick the version through the ordinary VM `image` field (spec/15). ---
	#
	# v16 is the current/default line: it keeps the warm entrypoint (it doubles as the
	# self-serve site accelerator base) and `registers_as=default_bench_snapshot`, so
	# an auto-registered v16 warm bake stays the self-serve golden — the existing
	# behaviour, unchanged. v15 + nightly are COLD customer goldens (no warm, no
	# register): promote-to-image requires cold, and only one warm golden registers
	# per server.
	"bench-v16": _bench_variant(
		"bench-v16",
		"Golden bench v16",
		frappe_branch="version-16",
		erpnext_branch="version-16",
		python_version="3.14",
		registers_as="default_bench_snapshot",
		warm_entrypoint="warm.sh",
	),
	# Frappe v15 predates Python 3.14; it runs on 3.11 (uv fetches the interpreter,
	# so the host needn't preinstall it — python_env_manager runs `uv venv --python
	# 3.11`). v15+python compat is the one host fact still unproven until a real bake
	# (spec/15 release gate).
	"bench-v15": _bench_variant(
		"bench-v15",
		"Golden bench v15",
		frappe_branch="version-15",
		erpnext_branch="version-15",
		python_version="3.11",
	),
	# Nightly tracks the moving `develop` of both Frappe and ERPNext. The bake records
	# the resolved commit SHAs into the Image Build for traceability (image_build.run),
	# since the inputs float.
	"bench-nightly": _bench_variant(
		"bench-nightly",
		"Golden bench nightly (develop)",
		frappe_branch="develop",
		erpnext_branch="develop",
		python_version="3.14",
	),
	# --- The admin-console line. Same three Frappe versions, but baked in `admin`
	# mode: build.sh skips `new-site` + ERPNext entirely and leaves only the bench +
	# the bench-cli admin app running (a Flask management console, NOT a Frappe site).
	# A clone's first-boot deploy sets `[admin].domain = <fqdn>` + `bench setup production`
	# so the FQDN maps to the admin app (deploy-site.py `--mode admin`); its readiness
	# probe is the admin app's `/api/status`, not the Frappe `/api/method/ping`
	# (spec/08, [[atlas-admin-mode-health-path]]). These are COLD goldens — no warm
	# entrypoint (warm is the self-serve *site* accelerator's concern) and no
	# `registers_as` (the admin image is a distinct product, never the
	# `default_bench_snapshot` a self-serve site clones). They promote to their own
	# series name (`bench-v16-admin` etc.) so the Central catalog links them by
	# name-match alongside the site variants; a customer picks an admin VM through the
	# ordinary `image` field. ---
	"bench-v16-admin": _bench_variant(
		"bench-v16-admin",
		"Golden bench v16 (admin)",
		frappe_branch="version-16",
		erpnext_branch="version-16",
		python_version="3.14",
		build_mode="admin",
	),
	"bench-v15-admin": _bench_variant(
		"bench-v15-admin",
		"Golden bench v15 (admin)",
		frappe_branch="version-15",
		erpnext_branch="version-15",
		python_version="3.11",
		build_mode="admin",
	),
	"bench-nightly-admin": _bench_variant(
		"bench-nightly-admin",
		"Golden bench nightly admin (develop)",
		frappe_branch="develop",
		erpnext_branch="develop",
		python_version="3.14",
		build_mode="admin",
	),
	"proxy": ImageRecipe(
		name="proxy",
		title="Reverse proxy image",
		source_directory="proxy",
		build_entrypoint="build.sh",
		remote_directory="/tmp/nginx-build",
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
	"sshpiper": ImageRecipe(
		name="sshpiper",
		title="SSHPiper ingress image",
		source_directory="sshpiper",
		build_entrypoint="build.sh",
		remote_directory="/tmp/sshpiper-build",
		disk_gigabytes=10,
		memory_megabytes=512,
		vcpus=1,
		snapshot_title="sshpiper-image",
		task_script="sshpiper-build",
		exclude=("test",),
		finalize=_finalize_sshpiper,
		is_sshpiper=True,
	),
}


# Back-compat aliases: the old single `bench` recipe split into three versioned
# variants, so `bench` now resolves to `bench-v16` (the current line, the drop-in
# successor). Callers that still say `"bench"` (bootstrap, warm_restore e2e, older
# tests) keep working; the alias is deliberately NOT a Select option (recipe_names),
# so the operator only ever picks an explicit version. Remove the alias once every
# caller is updated.
_ALIASES = {"bench": "bench-v16"}


def get_recipe(name: str) -> "ImageRecipe":
	name = _ALIASES.get(name, name)
	if name not in RECIPES:
		frappe.throw(f"Unknown image recipe {name!r}; known: {sorted(RECIPES)}")
	return RECIPES[name]


def recipe_names() -> list[str]:
	"""The recipe keys an operator picks, for the Image Build `recipe` Select options.
	Real recipes only — the back-compat `bench` alias is intentionally excluded so the
	operator always selects an explicit version (bench-v15 / bench-v16 / bench-nightly)."""
	return list(RECIPES)
