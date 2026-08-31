<div align="center">
  <img src=".github/assets/logo.svg" alt="Atlas" width="80" height="80">
  <h1>Atlas</h1>
</div>

VM Management service of Frappe Cloud V2

### Layout

- `atlas/` — Frappe app (control plane: DocTypes, APIs, background jobs)
- `metal/` — `metald`, the Go host agent that manages Firecracker VMs
- `services/wg-mesh/` — WireGuard mesh + eBPF tenant isolation
- `services/http-proxy/` — OpenResty edge proxy and its control daemon

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app atlas
```

The services under `metal/` and `services/` are deployed independently of the
Frappe app; see the `docs/` directory inside each for setup instructions.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/atlas
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

The formatters are scoped to the Frappe app (`atlas/`); the standalone services
keep their own style.

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.
- Atlas HTTP proxy tests: Runs the `services/http-proxy` test suite when that service changes.

### License

agpl-3.0
