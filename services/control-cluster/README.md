# Atlas control cluster

The Python package `atlas_control` gives a service control daemon its caller authentication and its regional cluster. The [HTTP proxy](../http-proxy/README.md) and the [WireGuard gateway](../wg-gateway/README.md) use it. Each service package ships a copy below `<package>/control-cluster/`.

- How the cluster behaves: [high availability](../../docs/networking/http-proxy/high-availability.md)
- How callers authenticate: [control daemon authentication](../../docs/networking/http-proxy/control-daemon.md#authentication)
- Code contract: [SPEC.md](SPEC.md)

Atlas control cluster uses the [AGPL-3.0 license](../../license.txt).
