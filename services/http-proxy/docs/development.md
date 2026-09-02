# Development

Use this guide to change the proxy or its tests.

## Read these files first

Read these files before you change code:

1. `README.md`
2. `docs/setup.md`
3. `docs/control-daemon.md`
4. `docs/openresty.md`
5. `nginx/nginx.conf`
6. `control/proxy_control/main.py`
7. The Lua file for the route that you change.
8. The related test file in `tests`.

## Folder tree

```text
build-image.sh                    Build `proxy.ext4` with Firecracker.
run-image.sh                      Boot a built image with Firecracker.

control/
  proxy_control/
    main.py                       FastAPI API and startup configuration.
    auth.py                       Password and JWKS bearer token authentication.
    certificates.py               Validate and install wildcard certificates.
    client.py                     Send HTTP requests through the Unix socket.
    imds.py                       Read user data from the instance metadata service.
    mappings.py                   Read and change route maps.
    server.py                     Start the IPv4 and IPv6 daemon listeners.
  tests/                          Unit tests for the control daemon.
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

Change the matching file for API, authentication, metadata, map, or certificate changes:

- `main.py`, `auth.py`, `imds.py`, `mappings.py`, or `certificates.py`

Change `nginx/lua/http/admin.lua` when you change map storage or the private OpenResty API.

Change `nginx/lua/http/router.lua` or `nginx/lua/http/plain_router.lua` for HTTP routing.

Change `nginx/lua/stream` for custom-domain TLS routing. Change both HTTP and stream code for custom-domain map changes.

Change `nginx/setup.sh` for installed files, packages, users, paths, or systemd units. Update `tests/docker/Dockerfile` when setup files move.

Change `PYTHON_VERSION` in `nginx/setup.sh`, `requires-python` in `control/pyproject.toml`, `target-version` in `ruff.toml`, and the CI Python version together.

Update `build-image.sh` for image packages or build steps. Update `run-image.sh` for Firecracker, network, or MMDS changes. Update the rootfs URL and checksum together.

## Boot the proxy locally with firecracker

`build-image.sh` verifies the Ubuntu 24.04 minimal image, starts a Firecracker VM with 2 vCPUs and 1024 MiB RAM, runs `nginx/setup.sh`, and saves `proxy.ext4`. 

```sh
sudo ./build-image.sh
sudo ./run-image.sh
```

The local VM uses `100.64.0.2` with gateway `100.64.0.1`, a host tap device, and NAT. `build-image.sh` uses `100.64.1.x`, so both VMs can run together. The daemon is at `http://100.64.0.2:9000`; the proxy uses ports `80` and `443`. `Ctrl-C` stops the VM and removes its tap device.

`run-image.sh` requires `proxy.ext4`, then prompts for a proxy password unless `-p` supplies one. It writes the hash to `/etc/atlas/proxy-control.htpasswd` in the temporary boot copy only.

`run-image.sh` creates `.dev/id_ed25519` if needed and adds its public key to the temporary `frappe` account. It generates unique host keys, prints the SSH command, and leaves the image unchanged.

Use `-j` and `-a` for JWKS MMDS keys. These options test the IMDSv2 and MMDS v2 path. See [Control daemon](control-daemon.md).

```sh
sudo ./run-image.sh -j https://issuer.example.com/jwks.json -a atlas-proxy-control -p a-dev-password-hash
```

## Run the tests

Run the control daemon tests from `control/`. They do not need Docker.

```sh
pip install -e ".[test]"
python -m pytest -q tests
```

Run the integration tests from the service root. Docker must run without `sudo`.

```sh
docker compose -f tests/docker/docker-compose.yml up --build -d
python3 -m pytest -q tests/test_build.py tests/test_proxy.py tests/test_custom_domain_proxy.py tests/test_latency.py
docker compose -f tests/docker/docker-compose.yml down -v
```

Run a focused test for one area:

```sh
python3 -m pytest -q tests/test_proxy.py -k wildcard
python3 -m pytest -q tests/test_custom_domain_proxy.py
python3 -m pytest -q tests/test_build.py
```

## Check a change

```sh
bash -n nginx/setup.sh build-image.sh run-image.sh
python3 -m py_compile control/proxy_control/*.py tests/test_build.py tests/test_proxy.py tests/test_custom_domain_proxy.py tests/test_latency.py
git diff --check
```

Build the Docker image after setup, package, or installed-file changes.

```sh
docker compose -f tests/docker/docker-compose.yml build proxy
```

## Files that the proxy writes

OpenResty writes route maps below `/var/lib/nginx`. Do not edit those map files while OpenResty runs.

The control daemon writes the region file and wildcard certificate files. Use the control API for these changes.
