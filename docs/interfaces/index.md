# APIs and access

Atlas has four APIs. Each has a rules page and a generated reference.

| API | Caller | Rules | Reference |
| --- | --- | --- | --- |
| Atlas tenant API | Tenants, Central, and regional services | [Tenant API](tenant-api.md) | [Atlas API](/api/atlas/) |
| Metal control API | The Atlas app | [Metal controller contract](metal-contract.md) | [Metal API](/api/metal/) |
| HTTP proxy control API | Central and the Atlas app | [Control daemon](../networking/http-proxy/control-daemon.md) | [Proxy API](/api/http-proxy/) |
| WireGuard gateway API | Central | [WireGuard gateway](../networking/wireguard-gateway.md) | [WireGuard gateway API](/api/wg-gateway/) |

Start with [signing keys and tokens](signing-keys.md) to see how Central and Atlas share public keys while each service keeps its own permissions. The [security model](security.md) lists other control boundaries. [VM state updates](vm-state-updates.md) explains messages Atlas sends to Central. [API clients](api-clients.md) explains generated Python clients.
