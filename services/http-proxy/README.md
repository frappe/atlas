# Atlas HTTP proxy

The regional entry point for public HTTP and HTTPS traffic. One to five nodes route sites and custom domains to VM mesh addresses. OpenResty serves traffic. A control daemon stores and replicates the routes.

- How it works: [HTTP proxy](../../docs/networking/http-proxy/index.md)
- Install and configure: [install guide](../../docs/networking/http-proxy/install.md)
- Develop and test: [HTTP proxy development](../../docs/develop/http-proxy.md)
- Code contract: [SPEC.md](SPEC.md)

| Path | Contents |
| --- | --- |
| `nginx/` | OpenResty configuration, Lua code, setup, and systemd units |
| `control/proxy_control/` | Control API and the replicated route state. Clustering and authentication come from [control-cluster](../control-cluster/README.md). |
| `control/tests/`, `tests/` | Control daemon and data-plane tests |

Atlas HTTP proxy uses the [AGPL-3.0 license](../../license.txt).
