# API clients

`clients/` holds three generated Python clients and the OpenAPI documents that they come from. [openapi-python-client](https://github.com/openapi-generators/openapi-python-client) generates them. The clients are in Git, so a user can install one directly.

`clients/atlas-cli` provides `atlasctl`, an operator CLI that uses the generated Atlas client.

| Directory | Package | API |
| --- | --- | --- |
| `clients/atlas-client` | `atlas_client` | Atlas tenant API |
| `clients/atlas-proxy-client` | `atlas_proxy_client` | HTTP proxy control API |
| `clients/atlas-wg-gateway-client` | `atlas_wg_gateway_client` | WireGuard gateway API |

The [Atlas API](/api/atlas/), [HTTP proxy control API](/api/http-proxy/), and [WireGuard gateway API](/api/wg-gateway/) reference pages render these OpenAPI documents at build time.

## Regenerate

`scripts/generate-api-client.py` writes the OpenAPI document and then the client. It imports the application and reads the specification in memory. A site, a database, and a running server are not necessary.

```bash
pip install openapi-python-client==0.29.1
./scripts/generate-api-clients.sh
```

`scripts/generate-api-clients.sh` writes all clients. It builds `atlas-client` with the bench Python, which it finds two directories above the repository. It builds `atlas-proxy-client` and `atlas-wg-gateway-client` with `uv`, which supplies the daemon packages. Set `ATLAS_PYTHON` when the bench environment is in another location. To write one client, call the Python script directly.

```bash
python scripts/generate-api-client.py atlas-client
python scripts/generate-api-client.py atlas-proxy-client
python scripts/generate-api-client.py atlas-wg-gateway-client
```

Commit the result. The `atlas-client` command needs `frappe` and the `atlas` app on the Python path, so run it from a bench environment. The `atlas-proxy-client` command needs the `atlas-proxy-control` package. It writes a temporary configuration file, because `proxy_control.main` loads its configuration at import time. The `atlas-wg-gateway-client` command needs the `atlas-wg-gateway` package and writes a temporary configuration for the same reason.

`.gitattributes` marks the client packages and the OpenAPI documents as `linguist-generated`. GitHub collapses their diffs in a pull request and keeps them out of the language statistics.

The generator uses no post hooks. It does not lint or format its output, so the committed client is the same for every environment.

The client method names come from the OpenAPI operation IDs. Atlas uses the route function name. The HTTP proxy control and WireGuard gateway daemons use `generate_unique_id_function` to do the same.

## CI

The `Tests` workflow regenerates each client in the job of its component: `Atlas`, `HTTP proxy`, and `WireGuard gateway`. Each job fails when the result is different from the committed client. Regenerate the client in the same commit as an API change.

## Operator CLI

`atlasctl` lists hosts, inspects VMs, and requests VM lifecycle actions through the Atlas API. Atlas checks the token permissions and tenant for each request.

From the repository root, install both packages in a Python 3.11 or newer environment:

```bash
python -m pip install ./clients/atlas-client ./clients/atlas-cli
```

Set `ATLAS_BASE_URL` to the Atlas site URL, `ATLAS_TOKEN` to a Bearer token, and `ATLAS_TENANT_ID` to the tenant ID. The CLI reads the token from the environment. See [API authentication](security.md) for token requirements.

```bash
export ATLAS_BASE_URL=https://atlas.example.com
export ATLAS_TENANT_ID=7
atlasctl vms list --json
atlasctl vms get vm-7-00001
atlasctl vms restart vm-7-00001
```

Host listing requires a token with `scope=*` and tenant 0. A Central token can select tenant 0 with `atlasctl --tenant-id 0 hosts list`. Use `--base-url` and `--tenant-id` before the resource name to override the environment.

| Command | Operation |
| --- | --- |
| `hosts list` | List hosts that are not deleted. |
| `vms list` | List VMs for the selected tenant. |
| `vms get <id>` | Read one VM and its current state. |
| `vms start/stop/pause/resume/restart <id>` | Request one lifecycle action. |

List commands read all pages. `--limit` sets the page size from 1 to 100, and `--tag key:value` selects matching resources. Use comma-separated pairs to require several tags. `--json` prints one JSON object. List output has an `items` array. Other commands print the API response object.

Lifecycle actions report that the request was accepted. Use `vms get <id>` to check completion. Each HTTP request has a 30-second timeout. Requests are not retried. After a timeout, check the VM state before you repeat an action.

The CLI returns exit status 0 on success and 2 on failure. Errors go to standard error. It uses the Atlas API for these commands and does not connect to hosts through SSH.
