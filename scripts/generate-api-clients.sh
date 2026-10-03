#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GENERATE="$ROOT/scripts/generate-api-client.py"
GENERATOR_VERSION="0.29.1"
ATLAS_PYTHON="${ATLAS_PYTHON:-$ROOT/../../env/bin/python}"

if [ ! -x "$ATLAS_PYTHON" ]; then
	echo "Set ATLAS_PYTHON to a bench environment python that imports frappe and atlas." >&2
	exit 1
fi

if ! command -v openapi-python-client >/dev/null; then
	echo "Install openapi-python-client==$GENERATOR_VERSION." >&2
	exit 1
fi

if ! command -v uv >/dev/null; then
	echo "Install uv. It supplies the environments of the HTTP proxy control and WireGuard gateway daemons." >&2
	exit 1
fi

"$ATLAS_PYTHON" "$GENERATE" atlas-client
uv run --no-project --with "$ROOT/services/control-cluster" --with "$ROOT/services/http-proxy/control" python "$GENERATE" atlas-proxy-client
uv run --no-project --with "$ROOT/services/control-cluster" --with "$ROOT/services/wg-gateway/daemon" python "$GENERATE" atlas-wg-gateway-client
