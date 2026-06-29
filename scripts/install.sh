#!/bin/sh
# Create the host's Atlas interpreter — a uv-managed virtualenv on a uv-controlled
# CPython — and `uv pip install` the atlas package into it, exposing its `atlas`
# console script on PATH. A direct POSIX-sh port of bootstrap-server.py's former
# ensure_atlas_env(); it is the SINGLE SOURCE OF TRUTH for UV_VERSION / PY_VERSION.
#
# WHY THIS IS A SHELL SCRIPT, NOT A TASK. Every other Python verb runs as
# `atlas <verb>` on this venv. bootstrap-server used to be the lone carve-out — it
# CREATES the venv, so it could not itself require it, and so it ran on the host's
# stock /usr/bin/python3. Pulling the interpreter setup out into this script kills
# that carve-out: the controller runs install.sh over SSH FIRST (Server.bootstrap,
# right after the upload), the venv + console script then exist, and the bootstrap
# Task runs as a normal `atlas bootstrap-server` verb on the venv like everything
# else. POSIX sh (not bash) so it runs on any host's /bin/sh with no extra deps.
#
# It assumes the atlas package is ALREADY at /var/lib/atlas/bin (the controller
# scp'd it via Server._bootstrap_uploads BEFORE this runs). It is NOT a
# code-transport mechanism — its only network fetch is the one pinned `curl` to
# astral.sh for uv. Idempotent (mirroring the Python it replaces): re-running
# re-converges the venv. Any failed step aborts non-zero so a broken venv fails
# the bootstrap HERE, before the systemd units are pointed at it.
set -eu

# --- the host's Atlas interpreter (a uv-managed venv on CPython) ---
# uv is a normal host tool: this installs it, then creates a virtualenv on a
# uv-controlled CPython and `uv pip install`s the atlas package into it. Every
# OTHER Task and every VM-boot hook then runs that venv's python (the runner's
# ATLAS_PYTHON, the systemd units). This is what frees Atlas from the host's
# `python3` version — controller and host run the same CPython no matter what
# Ubuntu shipped.
UV_VERSION="0.9.30"          # PINNED exact, not "latest" (the install URL embeds it)
PY_VERSION="3.14.3"          # the CPython uv installs into the venv

# Host path layout — must match scripts/lib/atlas/paths.py (ATLAS_VENV /
# ATLAS_PYTHON / ATLAS_CLI / BIN_DIRECTORY). The two trees don't share imports, so
# the literals are repeated; keep them in sync.
ATLAS_ROOT="/var/lib/atlas"
BIN_DIRECTORY="${ATLAS_ROOT}/bin"
UV_DIR="${ATLAS_ROOT}/uv"      # uv binary + its managed interpreters, under one root
ATLAS_VENV="${ATLAS_ROOT}/venv"
ATLAS_PYTHON="${ATLAS_VENV}/bin/python"
ATLAS_CLI="${ATLAS_VENV}/bin/atlas"
UV="${UV_DIR}/uv"

# 1. Install the PINNED uv to the fixed dir (the one genuine network fetch).
#    UV_UNMANAGED_INSTALL governs the uv BINARY's location (no PATH/profile edits);
#    UV_INSTALL_DIR is where it lands. The version is in the URL, so this never
#    silently rolls forward.
sudo env "UV_INSTALL_DIR=${UV_DIR}" UV_UNMANAGED_INSTALL=1 sh -c \
	"curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh"

# 2. Create the venv on a uv-controlled CPython (uv fetches the interpreter if
#    absent — kept inside the single /var/lib/atlas/uv tree). `uv venv` is
#    idempotent on an existing venv of the same Python.
sudo env "UV_PYTHON_INSTALL_DIR=${UV_DIR}" "${UV}" venv --python "${PY_VERSION}" "${ATLAS_VENV}"

# 3. Install the atlas package into the venv from the durable tree the caller
#    already scp'd to /var/lib/atlas/bin. This materialises the `atlas` console
#    script at ${ATLAS_VENV}/bin/atlas (its pyproject declares
#    atlas = atlas._cli:main). `--reinstall` so a re-bootstrap after a code edit
#    refreshes the install.
sudo env "VIRTUAL_ENV=${ATLAS_VENV}" "${UV}" pip install --reinstall "${BIN_DIRECTORY}"

# 4. Expose the console script on PATH for an operator (login shells AND sudo),
#    no profile edits — /usr/local/bin is FHS-correct for local admin binaries.
sudo ln -sfn "${ATLAS_CLI}" /usr/local/bin/atlas

# 5. DEEP sanity gate (the safety). A green `import atlas` does NOT prove the units
#    run, so exercise what they ACTUALLY do: atlas-pool.service's inline
#    `from atlas.lvm import ThinPool` (the largest module, likeliest stdlib gap on
#    a fresh interpreter), that the 4 firecracker-vm@.service boot hooks PARSE on
#    the venv python (py_compile), and that the `atlas` console script dispatches.
#    A broken venv must fail the install HERE — before the units are uploaded to
#    point at it.
version="$("${ATLAS_PYTHON}" --version)"
case "${version}" in
	*"${PY_VERSION}"*) ;;
	*) echo "Atlas venv python is '${version}', expected ${PY_VERSION}" >&2; exit 1 ;;
esac
"${ATLAS_PYTHON}" -c "import sys; sys.path.insert(0, '${BIN_DIRECTORY}'); from atlas.lvm import ThinPool"
"${ATLAS_PYTHON}" -m py_compile \
	"${BIN_DIRECTORY}/vm-disk-up.py" \
	"${BIN_DIRECTORY}/vm-network-up.py" \
	"${BIN_DIRECTORY}/vm-network-down.py" \
	"${BIN_DIRECTORY}/vm-restore.py"
"${ATLAS_CLI}" --help >/dev/null

echo "Atlas venv ready: ${version}"
