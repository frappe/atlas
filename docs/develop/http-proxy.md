# HTTP proxy development

::: warning Local setup is a work in progress
The local run steps below are not fully verified. Ask the team if a step fails.
:::

The HTTP proxy has two parts. OpenResty and Lua serve public traffic from local route maps. A Python control daemon validates route changes and copies them to other nodes. Atlas creates the proxy VMs and writes their configuration. Read the [service overview](../networking/http-proxy/index.md) before you change a request path.

## Find the owner

| Change | Read first | Code |
| --- | --- | --- |
| Public request path | [OpenResty routes](../networking/http-proxy/openresty.md) | [HTTP Lua](../../services/http-proxy/nginx/lua/http/router.lua), [TLS Lua](../../services/http-proxy/nginx/lua/stream/) |
| Route API or authorization | [Control daemon](../networking/http-proxy/control-daemon.md) | [API](../../services/http-proxy/control/proxy_control/main.py), [authentication](../../services/control-cluster/atlas_control/auth.py) |
| Cluster membership or replication | [High availability](../networking/http-proxy/high-availability.md) | [Cluster](../../services/control-cluster/atlas_control/cluster.py), [route state](../../services/http-proxy/control/proxy_control/routes.py) |
| Node configuration or install | [Install](../networking/http-proxy/install.md) | [Configuration](../../services/http-proxy/control/proxy_control/config.py), [Atlas setup](../../atlas/service/core/proxy/configuration.py) |

Read the [component specification](../../services/http-proxy/SPEC.md) for interfaces and ownership. The [code map](code-map.md) links this service to the rest of Atlas.

## Source layout

```text
control/
  proxy_control/
    main.py                 FastAPI map API and startup wiring.
    config.py               Read /etc/atlas/proxy-control.toml.
    apply.py                The proxy-control command.
    certificates.py         Validate and install wildcard certificates.
    client.py               Send HTTP requests through the Unix socket.
    mappings.py             Read and change route maps.
    routes.py               The route state that the cluster replicates.
    server.py               Start the IPv4 and IPv6 daemon listeners.
  tests/                    Unit tests for the control daemon.
  pyproject.toml            Python package and the proxy-control command.

nginx/
  setup.sh                  Install the proxy on Ubuntu.
  nginx.conf                Main OpenResty configuration.
  lua/http/                 HTTP routes, maps, and map storage.
  lua/stream/               TLS SNI routes and the SNI bridge.
  pages/                    HTML error pages.
  systemd/                  OpenResty and daemon systemd units.

tests/
  test_proxy.py             Site route tests.
  test_custom_domain_proxy.py  Custom-domain tests.
  test_build.py             Image and install tests.
  test_latency.py           Route and map size tests.
```

## Make a change

Change the matching file for API, route state, configuration, map, or certificate changes:

- `main.py`, `routes.py`, `config.py`, `apply.py`, `mappings.py`, or `certificates.py`

Authentication and clustering live in the shared [control-cluster](../../services/control-cluster/README.md) package. A change there also affects the WireGuard gateway.

When you add a configuration key, update `config.py`, [the install guide](../networking/http-proxy/install.md), and `atlas/service/core/proxy/configuration.py`. Atlas writes the installed configuration.

Change `nginx/lua/http/admin.lua` when you change map storage or the private OpenResty API.

Change `nginx/lua/http/router.lua` or `nginx/lua/http/plain_router.lua` for HTTP routing.

Change `nginx/lua/stream` for custom-domain TLS routing. Change both HTTP and stream code for custom-domain map changes.

Change `nginx/setup.sh` for installed files, packages, users, paths, or systemd units. Keep the install tests in sync when setup files move.

Change `PYTHON_VERSION` in `nginx/setup.sh`, `requires-python` in `control/pyproject.toml`, `target-version` in `ruff.toml`, and the CI Python version together.

## Run the proxy locally

Run these commands from `services/http-proxy/`. Create a virtual environment and install the control package with its test dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --editable ../control-cluster --editable 'control[test]'
```

Start the control daemon with its default configuration:

```sh
python -m proxy_control.main
```

The daemon listens on port `9000`. Install OpenResty with [Setup](../networking/http-proxy/install.md) before you use the readiness or map endpoints.

## Run the tests

Run the control daemon tests from the service root:

```sh
python -m pip install --editable ../control-cluster --editable 'control[test]'
python -m pytest -q control/tests
```

Run a focused test for one area:

```sh
python -m pytest -q control/tests/test_config.py
python -m pytest -q control/tests/test_apply.py
python -m pytest -q control/tests/test_routes.py
python -m pytest -q ../control-cluster/tests
```

## Check a change

```sh
bash -n nginx/setup.sh
python3 -m compileall -q control/proxy_control control/tests
git diff --check
```

## Files that the proxy writes

OpenResty writes route maps below `/var/lib/nginx`. The control daemon writes `cluster-state.json` in the same directory. Do not edit these files while the services run.

The `proxy-control` command writes the region file and the wildcard certificate files. Change `/etc/atlas/proxy-control.toml` and run the command again. Do not edit its output files.
