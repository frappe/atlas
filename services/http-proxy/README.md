# Atlas HTTP proxy

Atlas HTTP proxy is a VM image for one Atlas region. It routes site traffic and custom-domain traffic to site VMs over IPv6.

Use the control daemon for normal configuration. The daemon listens on IPv4 and IPv6 port `9000` by default.

Read these documents in this order:

1. [Setup](docs/setup.md)
2. [Control daemon](docs/control-daemon.md)
3. [OpenResty](docs/openresty.md)
4. [Development](docs/development.md)

The control daemon is the normal API. OpenResty has a private Unix socket API. Do not expose that socket to a network.

## Main paths

```text
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

The setup script creates a placeholder certificate and an empty authentication file. The proxy can start before the controller sends a real region, certificate, or map.

Follow the [setup guide](docs/setup.md) to configure the proxy.

## License

Atlas WG Mesh is licensed under [AGPL-3.0](../../license.txt).
