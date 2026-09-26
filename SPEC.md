# Atlas Repository Specification

Atlas is a monorepo for Frappe Cloud V2 VM infrastructure. It holds one Frappe app and five components in one Git repository.

## Components

| Component | Path | Owns |
|---|---|---|
| [Atlas app](atlas/SPEC.md) | `atlas/` | Frappe app |
| [Metal](metal/SPEC.md) | `metal/` | Host VM management |
| [HTTP proxy](services/http-proxy/SPEC.md) | `services/http-proxy/` | Regional proxy |
| [IPv6 router](services/ipv6-router/SPEC.md) | `services/ipv6-router/` | Public IPv6 translation |
| [WG Mesh](services/wg-mesh/SPEC.md) | `services/wg-mesh/` | Private VM network |
| [WG gateway](services/wg-gateway/SPEC.md) | `services/wg-gateway/` | Customer access to tenant VMs |

`clients/` holds the generated [API clients](docs/interfaces/api-clients.md). `llm/` and `.greptile/` hold review rules. `.vitepress/` builds the docs site.

## Rules

- Keep component code inside its component. Use an API or contract across components.
- The root has no `go.mod` or `go.work`. Run Go commands inside the component.

## Continuous integration

- `pages.yml` publishes the VitePress site on a push to `develop`.
- `tests.yml` and `linter.yml` have no path filter, so required checks always report.
- Each `tests.yml` job skips its steps when `.github/scripts/changed-paths.sh` finds no match. Include `tests.yml` and the script in each pattern.

| Job | Runs for |
|---|---|
| Atlas | Every pull request |
| Metal | `metal/` |
| WG Mesh | `services/wg-mesh/` |
| HTTP proxy | `services/http-proxy/` and the proxy API client |
