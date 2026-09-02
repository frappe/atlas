# Atlas HTTP proxy

Atlas HTTP proxy is a VM image for one region. It routes site and custom-domain traffic to site VMs over IPv6.

Use the control daemon for configuration. It listens on IPv4 and IPv6 port `9000` by default.

Read these documents in this order:

1. [Setup](docs/setup.md)
2. [Control daemon](docs/control-daemon.md)
3. [OpenResty](docs/openresty.md)
4. [Development](docs/development.md)

The control daemon is the public API. OpenResty uses a private Unix socket. Do not expose it to a network.

## Main paths

```text
build-image.sh                  Build `proxy.ext4` with Firecracker.
run-image.sh                    Boot a built image with Firecracker.
nginx/setup.sh                  Set up a new Ubuntu VM image.
nginx/nginx.conf                Configure OpenResty.
nginx/lua/http/                 Route HTTP traffic and store HTTP maps.
nginx/lua/stream/               Route TLS traffic by SNI.
nginx/pages/                    Store the HTML error pages.
nginx/systemd/                  Store the systemd units.
control/proxy_control/          Store the control daemon package.
control/pyproject.toml          Define Python packages for the daemon.
docs/                           Store operator and developer guides.
tests/                          Store the Python test suites.
tests/docker/                   Store the Docker test environment.
```

## Quick start

```sh
sudo ./nginx/setup.sh
sudo systemctl enable --now openresty.service atlas-proxy-control.service
```

The setup script creates a placeholder certificate and an empty authentication file. The proxy can start before the controller sends its configuration.

Follow the [setup guide](docs/setup.md) to configure the proxy.

## Local testing with firecracker

```sh
sudo ./build-image.sh
sudo ./run-image.sh -j https://issuer.example.com/jwks.json -a atlas-proxy-control
```

`build-image.sh` boots the pinned Ubuntu 24.04 minimal image, runs `nginx/setup.sh`, and saves `proxy.ext4`. It removes build-only SSH access first. `run-image.sh` requires `proxy.ext4`. It writes the password into the boot copy and can seed JWKS through MMDS. See [Development](docs/development.md).

## License

Atlas WG Mesh is licensed under [AGPL-3.0](../../license.txt).
