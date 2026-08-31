# HTTP Proxy Component Specification

[Root specification](../../SPEC.md)

## Purpose

The HTTP proxy is a regional VM image. It routes site traffic to site VMs over IPv6.

## Layout

```text
control/                     Python control daemon
  proxy_control/             Control package
nginx/                       OpenResty and Nginx configuration
  lua/                       Routing logic
  pages/                     Error pages
  systemd/                   systemd units
tests/                       Python tests
  docker/                    Docker test environment
docs/                        Setup and operation docs
```

## Software

The control daemon uses Python 3.12. The data plane uses OpenResty, Nginx, Lua, and systemd.

## Interfaces

The control daemon uses TCP port `9000` by default. OpenResty uses a private Unix socket. Do not expose the socket to a network.

## Validation

From this directory, run `python -m pytest -q tests`. Use the Docker test stack when required.

## Documentation

Read [`README.md`](README.md) first. Then read the relevant file in [`docs/`](docs/).

## Scope

The proxy handles regional site traffic. It does not manage VM lifecycle or private VM networking.

## Ownership

Keep control code in `control/`. Keep data-plane code in `nginx/`. Keep tests in `tests/`.
