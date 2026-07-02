"""Golden bench image control plane — bake a bench-preinstalled image by building
INSIDE a plain guest over SSH, then snapshotting it.

This is the controller side of the golden bench image (spec/08-images.md). The
build itself — upload the committed `bench/` tree, run `build.sh` over guest-SSH
detached, record a Task, fail loud — is the shared `image_builder.run_build` seam;
`build_bench` is the thin wrapper that hands it the `bench` recipe — now a
back-compat alias `get_recipe("bench") → RECIPES["bench-v16"]` (the current line;
the single `bench` recipe split into versioned variants, image_recipes.py). The
full provision→build→stop→snapshot→register
lifecycle around it lives in the `Image Build` DocType.

That snapshot is the reusable "golden bench image" — a VM with bench-cli, the uv
venv, the Frappe clone, MariaDB + Redis, AND a fully-created site baked under the
fixed name `site.local`, so `deploy-site.py` (spec/14-self-serve.md) only RENAMES
that baked site to the per-VM FQDN (`bench rename-site`, which regenerates the nginx
vhost + re-runs production setup), never paying the multi-minute `bench new-site`
per signup.
"""

import frappe

from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.image_builder import run_build
from atlas.atlas.image_recipes import get_recipe
from atlas.atlas.ssh import connection_for_guest

SANITY_SITE = "site.local"
# Where build.sh installed bench-cli (mirrors bench/deploy-site.py BENCH_CLI_DIR /
# BENCH_NAME) — the site sanity probe invokes `bench browse` through it. ~/pilot
# since the frappe/bench-cli → frappe/pilot rename (install.sh fc89e51+ clones there).
SANITY_BENCH_CLI_DIR = "/home/frappe/pilot"
SANITY_BENCH_NAME = "atlas"
# The baked bench.toml on the build VM (mirrors bench/deploy-site.py BENCH_TOML). The
# admin sanity probe reads `[admin].port` from it to reach the internal admin gunicorn.
SANITY_BENCH_TOML = "/home/frappe/pilot/benches/atlas/bench.toml"


def build_bench(virtual_machine: str) -> None:
	"""Turn a freshly-provisioned Ubuntu guest into a golden bench: upload the
	committed `bench/` tree and run build.sh inside the guest (install bench-cli +
	`bench init` + bake a `site.local` site). After this returns the caller stops +
	snapshots the VM; that snapshot is the rollable golden image.

	Idempotent (build.sh re-runs cleanly), so this doubles as the "re-bake" verb.
	Recorded as a `bench-build` Task row for the audit trail, like every guest op."""
	run_build(virtual_machine, get_recipe("bench"))


# The in-guest sanity script. The assertions are printed on labelled lines we parse
# back out. The whole probe is a mode-specific block (`{mode_block}`) because the two
# modes serve over DIFFERENT endpoints on the build VM:
#
#  SITE — over `:80` with the baked-site Host header (the build VM serves on
#  `site.local`; the FQDN rename happens only at deploy), the same contract build.sh's
#  own in-bake ping gate uses:
#   * SERVE   — `/api/method/ping` answers 200 + "pong". Production stack up + serving.
#   * LOGIN   — `bench browse --user Administrator` (as the frappe user) mints a real
#               session (sid parsed from its printed Login URL — no `--sid` flag exists),
#               then that `sid` cookie authenticates a follow-up request:
#               200 + a `full_name` in the bootinfo. Catches a site that serves but
#               whose login path is broken — the gap the unauthenticated ping gate
#               misses. The baked Administrator password is a random bake-time secret
#               (build.sh) and is never known to this probe — proving auth works no
#               longer means knowing it.
#   * NEGCTL  — a GARBAGE sid is rejected (still counted as a guest, no `full_name`).
#               Without it a bootinfo endpoint that 200s on anything passes LOGIN
#               falsely; proves the session check is real.
#
#  ADMIN — the admin console is the bench-cli admin Flask app on the INTERNAL admin
#  gunicorn at `127.0.0.1:(<[admin].port>+1)`. At bake time `[admin].domain` is UNSET
#  (deploy-site.py sets it per clone), so NO `:80` admin vhost exists yet — `:80` is
#  just the default nginx server. So we probe the internal admin port directly (no
#  Host header needed; the admin app answers any), reading `[admin].port` from the
#  guest bench.toml:
#   * SERVE   — `/api/status` on the admin gunicorn answers 200. Admin stack serving.
#   * ADMINUI — GET `/` on the admin gunicorn returns 200 AND the served HTML shell
#               carries the `<title>Pilot</title>` marker (the admin console's own
#               index.html since the bench-cli → pilot rename). /api/status can be 200
#               while the console
#               page is blank/broken (bad asset build, 500 template) — this asserts the
#               admin URL actually RENDERS, which is what a customer hits after deploy.
_SANITY_SITE_BLOCK = r"""
set +e
H='Host: {site}'
echo '=== SERVE ==='
serve_code=$(curl -s -m 20 -o /tmp/atlas_sane_serve.out -w '%{{http_code}}' -H "$H" "http://127.0.0.1/api/method/ping")
echo "http_code=$serve_code"
echo "body=$(head -c 200 /tmp/atlas_sane_serve.out)"
echo '=== LOGIN ==='
browse_out=$(sudo -u frappe bash -lc "export PATH='{bench_cli_dir}':\$PATH; export XDG_RUNTIME_DIR=/run/user/\$(id -u); cd '{bench_cli_dir}'; '{bench_cli_dir}/bench' -b {bench_name} --site {site} browse --user Administrator")
sid=$(grep -oP 'sid=\K\S+' <<<"$browse_out")
login_code=$(curl -s -m 20 -o /tmp/atlas_sane_login.out -w '%{{http_code}}' -H "$H" -b "sid=$sid" "http://127.0.0.1/api/method/frappe.auth.get_logged_user")
echo "http_code=$login_code"
echo "body=$(head -c 200 /tmp/atlas_sane_login.out)"
echo "user=$(grep -c -w Administrator /tmp/atlas_sane_login.out)"
echo '=== NEGCTL ==='
bad_code=$(curl -s -m 20 -o /tmp/atlas_sane_bad.out -w '%{{http_code}}' -H "$H" -b 'sid=atlas-sanity-garbage-sid-xxxxx' "http://127.0.0.1/api/method/frappe.auth.get_logged_user")
echo "http_code=$bad_code"
echo "user=$(grep -c -w Administrator /tmp/atlas_sane_bad.out)"
"""

# Reads [admin].port from the guest bench.toml SECTION-AWARELY (awk: the first `port`
# key after the `[admin]` header, stopping at the next section), probes the admin
# gunicorn at port+1. `{bench_toml}` is the only substitution. The `%{{ }}`
# escapes survive the single .format() in sanity_check (collapse to `%{ }`).
_SANITY_ADMIN_BLOCK = r"""
set +e
admin_port=$(awk '/^\[admin\]/{{a=1;next}} /^\[/{{a=0}} a&&/^[[:space:]]*port[[:space:]]*=/{{gsub(/[^0-9]/,"");print;exit}}' {bench_toml})
echo "admin_port=$admin_port"
internal=$((admin_port + 1))
echo "internal_port=$internal"
echo '=== SERVE ==='
serve_code=$(curl -s -m 20 -o /tmp/atlas_sane_serve.out -w '%{{http_code}}' "http://127.0.0.1:$internal/api/status")
echo "http_code=$serve_code"
echo "body=$(head -c 200 /tmp/atlas_sane_serve.out)"
echo '=== ADMINUI ==='
adminui_code=$(curl -s -m 20 -o /tmp/atlas_sane_adminui.out -w '%{{http_code}}' "http://127.0.0.1:$internal/")
echo "http_code=$adminui_code"
echo "marker=$(grep -c -i '<title>Pilot</title>' /tmp/atlas_sane_adminui.out)"
"""


def sanity_check(virtual_machine: str, timeout_seconds: int = 120) -> dict:
	"""Post-build, pre-snapshot gate: prove the freshly-baked build VM actually
	SERVES and (site mode) that `bench browse` mints a session that really logs in,
	before the build is allowed to become a snapshot.

	build.sh's own in-bake gate only curls the unauthenticated `/api/method/ping`,
	so a build whose site serves but whose login path is broken still snapshots
	clean — and the break surfaces only when a customer can't log in. This closes
	that gap at the source: run it from `Image Build.run` right after `run_build`
	returns (the production stack is up, the VM still Running), and a miss raises →
	the build is marked Failed and never snapshots.

	Mode-specific: site mode probes `:80` (Host: site.local) for serve + a
	`bench browse`-minted session + a garbage-session rejection; admin mode probes
	the INTERNAL admin gunicorn (`[admin].port`+1, read from the guest bench.toml —
	at bake time no `:80` admin vhost exists yet, that is wired per clone at deploy)
	for serve (`/api/status`) AND that GET `/` renders the admin console page
	(`<title>Pilot</title>` marker, not a blank/500 shell). Returns the parsed
	result dict on success; raises
	frappe.ValidationError on any failed assertion or a non-zero SSH exit."""
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	mode = vm.build_mode or "site"

	# Build the in-guest probe for the mode. `.format()` runs once (collapsing each
	# block's `%{{http_code}}` → `%{http_code}` and resolving its args).
	if mode == "site":
		remote = _SANITY_SITE_BLOCK.format(
			site=SANITY_SITE, bench_cli_dir=SANITY_BENCH_CLI_DIR, bench_name=SANITY_BENCH_NAME
		)
	else:
		remote = _SANITY_ADMIN_BLOCK.format(bench_toml=SANITY_BENCH_TOML)

	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stdout, stderr, code = run_ssh(connection, key_path, remote, timeout_seconds=timeout_seconds)
	if code != 0:
		frappe.throw(
			f"Sanity check could not reach the build guest (SSH exit {code}): {(stderr or '')[-300:]}"
		)

	parsed = _parse_sanity(stdout or "", mode)
	failures = _sanity_failures(parsed, mode)
	if failures:
		frappe.throw(
			f"Build {virtual_machine} (mode={mode}) failed its post-build sanity check: "
			f"{'; '.join(failures)}\n--- guest output ---\n{(stdout or '')[-600:]}"
		)
	return parsed


def _parse_sanity(out: str, mode: str) -> dict:
	"""Pull the labelled `key=value` lines back out of the in-guest script's stdout."""

	def grab(marker: str, key: str) -> str:
		segment = out.split(marker, 1)[-1]
		for line in segment.splitlines():
			if line.startswith(key + "="):
				return line.split("=", 1)[1].strip()
		return ""

	parsed = {
		"mode": mode,
		"serve_http": grab("=== SERVE", "http_code"),
		"serve_body": grab("=== SERVE", "body"),
	}
	if mode == "site":
		parsed.update(
			{
				"login_http": grab("=== LOGIN", "http_code"),
				"login_body": grab("=== LOGIN", "body"),
				"login_user": grab("=== LOGIN", "user"),
				"negctl_http": grab("=== NEGCTL", "http_code"),
				"negctl_user": grab("=== NEGCTL", "user"),
			}
		)
	else:
		parsed.update(
			{
				"adminui_http": grab("=== ADMINUI", "http_code"),
				"adminui_marker": grab("=== ADMINUI", "marker"),
			}
		)
	return parsed


def _sanity_failures(parsed: dict, mode: str) -> list:
	"""Turn the parsed probe result into a list of human-readable failures (empty =
	pass). Site mode demands serve + an authenticated `bench browse` session +
	rejection of a garbage session; admin mode demands serve only."""
	failures = []
	# Serving: 200, and for site mode the body must actually be the pong (a 200 from
	# a wrong vhost / default server wouldn't carry it).
	if parsed["serve_http"] != "200":
		failures.append(f"does not serve (readiness HTTP {parsed['serve_http'] or 'no-response'})")
	elif mode == "site" and "pong" not in parsed["serve_body"].lower():
		failures.append(f"serves 200 but no pong (body: {parsed['serve_body'][:80]!r})")

	if mode != "site":
		# Admin console must RENDER, not merely health-check: GET / returns 200 and the
		# served HTML shell carries the `<title>Pilot</title>` marker. `/api/status` can
		# be 200 while the console page is blank/broken (bad asset build, 500 template).
		if parsed["adminui_http"] != "200":
			failures.append(
				f"admin console does not render (GET / HTTP {parsed['adminui_http'] or 'no-response'})"
			)
		elif parsed["adminui_marker"] in ("", "0"):
			failures.append(
				"admin console served 200 but the page is not the Pilot admin console (no marker)"
			)
		return failures

	# Login: `bench browse` minted a session (sid parsed from its printed Login
	# URL — there is no `--sid` flag) that authenticates a follow-up
	# request as Administrator — 200 + the session resolves to Administrator, not
	# Guest. Proves the browse/login path works without the probe ever knowing the
	# (random, bake-time) Administrator password.
	logged_in = parsed["login_http"] == "200" and parsed["login_user"] not in ("", "0")
	if not logged_in:
		failures.append(
			f"bench browse session did not authenticate "
			f"(HTTP {parsed['login_http']}, body: {parsed['login_body'][:80]!r})"
		)
	# Negative control: a garbage sid MUST resolve to Guest, not Administrator. If it
	# doesn't, the login pass above is meaningless (any session would pass).
	if parsed["negctl_user"] not in ("", "0"):
		failures.append("a garbage session was NOT rejected — login check is not trustworthy")
	return failures
