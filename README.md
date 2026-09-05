<div align="center">
  <img src=".github/assets/logo.svg" alt="Atlas" width="80" height="80">
  <h1>Atlas</h1>
</div>

Atlas is the virtual machine control system for Frappe Cloud V2.

This is a monorepo and contains the controller (atlas), the host daemon (metal), the HTTP proxy, and the private network mesh (Atlas WG Mesh). Each component has its own README and development instructions.

## Start here

| Component     | Purpose                                                 | First document                              |
| ------------- | ------------------------------------------------------- | ------------------------------------------- |
| Atlas app     | Provider, Server, placement, image, and user operations | [Atlas app](atlas/README.md)                |
| Metal         | Virtual machine state and host resources                | [Metal](metal/README.md)                    |
| HTTP proxy    | Regional HTTP and TLS routing                           | [HTTP proxy](services/http-proxy/README.md) |
| Atlas WG Mesh | Private virtual machine network                         | [Atlas WG Mesh](services/wg-mesh/README.md) |

Read the [architecture](docs/architecture.md) for ownership and system flows.
Read the [glossary](docs/glossary.md) for stable resource and state terms.

## Development

Each component owns its commands and tests. Do not run Go commands from the repository root.

- Use [Atlas development](atlas/docs/development.md) for the Frappe app.
- Use [Metal development and tests](metal/docs/testing.md) for the Go host daemon.
- Use each service README for its local commands.

Atlas publishes host binaries during installation and migration. Install the [Ubuntu build tools](atlas/SPEC.md#ubuntu-build-tools) first.

## License

Atlas uses the AGPL-3.0 license.
