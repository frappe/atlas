# API clients

`clients/` holds three generated Python clients and the OpenAPI documents that they come from. [openapi-python-client](https://github.com/openapi-generators/openapi-python-client) generates them. The clients are in Git, so a user can install one directly.

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
