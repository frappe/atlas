# Development

Use this guide when you change the proxy code or its tests.

## Read these files first

Read the files in this order before you change code.

1. `README.md`
2. `docs/setup.md`
3. `docs/control-daemon.md`
4. `docs/openresty.md`
5. `nginx/nginx.conf`
6. `control/proxy_control/main.py`
7. The Lua file for the route that you change.
8. The related test file in `tests`.

This order gives you the VM setup, the public API, the traffic path, and the test rules before you change code.

## Folder tree

```text
control/
  proxy_control/
    main.py                       FastAPI API, authentication, and Unix socket client.
    certificates.py               Validate and install wildcard certificates.
    client.py                     Send HTTP requests through the Unix socket.
    mappings.py                   Read and change route maps.
    server.py                     Start the IPv4 and IPv6 daemon listeners.
  pyproject.toml                  Python package list.
  atlas-proxy-control.env.example Example daemon environment file.

nginx/
  setup.sh                        Install the proxy on Ubuntu.
  nginx.conf                      Main OpenResty configuration.
  lua/domain_lookup.lua           Find an exact or wildcard custom domain.
  lua/http/                       HTTP routes, maps, and map storage.
  lua/stream/                     TLS SNI routes and the SNI bridge.
  pages/                          HTML error pages.
  systemd/                        OpenResty and daemon systemd units.

tests/
  test_proxy.py                   Site route tests.
  test_custom_domain_proxy.py     Custom-domain tests.
  test_build.py                   Image and install tests.
  test_latency.py                 Route and map size tests.
  docker/                         Docker Compose test environment.
```

## Change guide

Change `control/proxy_control/main.py` when you change the controller API or authentication. Change `control/proxy_control/mappings.py` when you change route map operations. Change `control/proxy_control/certificates.py` when you change certificate validation or installation.

Change `nginx/lua/http/admin.lua` when you change map storage or the private OpenResty API.

Change `nginx/lua/http/router.lua` and `nginx/lua/http/plain_router.lua` when you change HTTP site or domain routing.

Change `nginx/lua/stream` when you change custom-domain TLS routing. Change both HTTP and stream code for a custom-domain map change.

Change `nginx/setup.sh` when you change installed files, packages, users, paths, or systemd units. Update `tests/docker/Dockerfile` when you rename or move setup files.

## Run the tests

Docker must run without `sudo`. Run these commands from the service root.

```sh
docker compose -f tests/docker/docker-compose.yml up --build -d
python3 -m pytest -q tests/test_build.py tests/test_proxy.py tests/test_custom_domain_proxy.py tests/test_latency.py
docker compose -f tests/docker/docker-compose.yml down -v
```

The test suite expects the Docker Compose stack to run before `pytest` starts.

Run a focused test when you change one area.

```sh
python3 -m pytest -q tests/test_proxy.py -k wildcard
python3 -m pytest -q tests/test_custom_domain_proxy.py
python3 -m pytest -q tests/test_build.py
```

## Do a check before you send a change

```sh
bash -n nginx/setup.sh
python3 -m py_compile control/proxy_control/*.py tests/test_build.py tests/test_proxy.py tests/test_custom_domain_proxy.py tests/test_latency.py
git diff --check
```

Build the Docker image after you change the setup script, the daemon package list, or an installed file.

```sh
docker compose -f tests/docker/docker-compose.yml build proxy
```

## Files that the proxy writes

OpenResty writes route maps below `/var/lib/nginx`. Do not edit those map files while OpenResty runs.

The control daemon writes the region file and wildcard certificate files. Use the control API for these changes.
