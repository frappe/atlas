# Atlas app

::: warning Local setup is a work in progress
The site setup below is not fully verified. Use [development setup](index.md) to choose an environment, and ask the team if a step fails.
:::

The Atlas app is the regional control plane: a Frappe app in Python. Tenants call its API, and operators use Frappe Desk. It stores requests in MariaDB, queues background jobs for provider and host work, and sends desired VM state to Metal.

```text
Tenant API or Desk -> Atlas records and jobs -> Provider hosts and Metal
                              ^                          |
                              +-- capacity and VM state -+
```

Redis holds jobs, cache entries, and console tokens. The [code map](code-map.md) lists each module with its handbook page, specification, and tests.

| The app owns | Read |
| --- | --- |
| Placement and VM records | [Placement](../compute/placement.md), [VM records](../compute/vm-records.md), [migration](../compute/migration.md) |
| Images and public IPs | [Image records](../storage/image-records.md), [public IPs](../networking/public-ips.md) |
| Hosts, providers, and service VMs | [Hosts and providers](../region/hosts-and-providers.md), [service VMs](../region/service-vms.md), [configuration](../region/configuration.md) |
| Tenant API | [Tenant API](../interfaces/tenant-api.md) |

Use a local Frappe site for API and DocType changes. Host provisioning also needs provider resources and a reachable Metal host.

## Prepare a site

Run commands from the pilot bench and always name the site.

Keep workers active for host setup, service installation, transfers, and reconciliation. Restart them after Python changes so they load the updated modules. For a full setup, use [Set up a test region](test-region.md).

```sh
pilot new-site atlas.localhost
pilot --site atlas.localhost install-app atlas
```

## Static checks and tests

Use a dedicated test site. Do not use a development site for tests. The CI workflow installs Atlas, builds assets, and runs the complete app suite on a test site.

```sh
ruff check atlas
pilot --site TEST_SITE set-config allow_tests true
pilot --site TEST_SITE run-tests --app atlas
```

## Add a route

A router owns one prefix below `/api`.

1. Add a resource group in `atlas/api/router.py`.
2. Add routes in a matching file under `atlas/api/routes/`.
3. Import that file in `register_atlas_api`.

The `before_request` hook registers routes before Frappe matches a request.

```python
from pydantic import BaseModel

from atlas.api.core.base import ApiResult, StrictModel
from atlas.api.core.docs import api_docs
from atlas.api.router import machines


class MachinePayload(StrictModel):
	name: str
	cores: int = 1


class MachineResponse(BaseModel):
	id: str
	name: str
	cores: int


@machines.post("")
@api_docs(
	request_example={"name": "vm-1", "cores": 4},
	responses={201: {"description": "The machine is created."}},
)
def create_machine(payload: MachinePayload) -> ApiResult[MachineResponse]:
	"""Create a machine."""
	machine = MachineResponse(
		id="machine-1", name=payload.name, cores=payload.cores
	)
	return ApiResult(machine, status=201)
```

- `payload` decodes the JSON body. A `GET` or `HEAD` route cannot declare it.
- `query` decodes the query string into the annotated Pydantic model.
- A path parameter uses the name in the route pattern, such as `virtual_machine_id`.
- The router rejects raw dictionaries, raw lists, scalars, unions, and missing annotations when it registers the route.

| Return value | Response |
| --- | --- |
| Pydantic model | `200`. |
| `ApiResult[model]` | Custom status or headers. |
| `None` | `204`. |

Pass validated values to the domain service. The first docstring line becomes the OpenAPI summary. Regenerate the [API client](../interfaces/api-clients.md) with each API change.

## Host binaries

Atlas builds `metald` and the WG Mesh CLI after installation and migration. A build runs only when its source changes. Atlas publishes each build as a public File, stores the File link in Atlas Settings, and keeps earlier files available.

A host downloads the binary during `install-metald.sh`, so the file needs an address that the host can reach. Set `atlas_internal_url` in the site configuration to the Atlas listener on its mesh address. Atlas uses `atlas_base_url`, then the site URL, when the key is absent. See [Atlas access to hosts](../region/host-access.md#internal-url).

```json
"atlas_internal_url": "http://[fdaa:1::ffff:ffff:ffff:ffff]:8000"
```

Install the build tools before you install or migrate Atlas. `make` runs both builds. `clang`, `libbpf-dev`, and `linux-libc-dev` build the WG Mesh eBPF object.

```bash
sudo apt-get update
sudo apt-get install --yes make clang libbpf-dev linux-libc-dev
```

On macOS, install Docker instead. Atlas uses Docker to build eBPF.

The builder uses an installed Go toolchain when it is new enough for `metal/go.mod`. Otherwise it downloads Go. The builds also download Go modules. An offline machine needs an installed Go toolchain and a populated module cache. It also needs an internal APT mirror or approved local packages.

Build one binary by hand with these commands. Each command skips the build when the linked File and source hash are current. A missing tool stops the command or migration.

```bash
pilot --site SITE build-metald
pilot --site SITE build-wg-mesh
```

## Limits and recovery

Unit tests cannot validate provider reachability or Firecracker behavior. Full host checks need:

- Provider credentials, quota, and a private network.
- Reachable public IPv4 and root SSH.
- KVM, ZFS, systemd, iptables, and WG Mesh.

On a test host, check creation, setup retry, power actions, disk inventory, and deletion.

## Experimental

The placement simulators can run offline or against a live fleet. They are developer tools, not part of the Atlas request path. Use a test region for a live trial.

::: details Source code and tests

- [Atlas install hooks](../../atlas/atlas/core/install.py) set up the site.
- [Binary build code](../../atlas/atlas/core/host_binaries.py) publishes host artifacts.
- [Placement simulator](../../atlas/simulator/vm_placement/__main__.py) starts offline trials, while `live.py` starts live trials.
- [Atlas CI workflow](../../.github/workflows/tests.yml) runs the app suite.

:::
