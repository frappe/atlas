# Atlas API clients

`clients/atlas-client` and `clients/atlas-proxy-client` are generated. `scripts/generate-api-clients.sh` writes them. Read `docs/api-clients.md` for the generation steps.

## Authentication

The Atlas API reads a Bearer token. The `tenant` claim of the token decides the tenant. A Central token uses `tenant=*` and must also send the `X-Tenant-ID` header, which the generated client accepts as the optional `x_tenant_id` argument. A regional token can omit the header. If it sends the header, the value must match the token tenant.

`scripts/dev-jwks-server.py` mints a token for a local site. Start it, set `central_jwks_url` in Atlas Settings to its `/jwks` URL, and type the `admin_audience_id` of the site to get a token.

## Example: open a VM console

The console token is single use and expires after 60 seconds. The console page reads it from the URL fragment, so the token stays out of the server logs and out of the `Referer` header.

```python
from atlas_client import Client
from atlas_client.api.vm_actions import create_virtual_machine_console_token
from atlas_client.models import ConsoleTokenPayload, ConsoleTokenPayloadMode

BASE_URL = "https://atlas.localhost"
client = Client(base_url=BASE_URL, token=CENTRAL_TOKEN)

with client as client:
    console = create_virtual_machine_console_token.sync(
        "vm-00010",
        client=client,
        body=ConsoleTokenPayload(mode=ConsoleTokenPayloadMode.TTY),
        x_tenant_id=12,
    )

print(f"{BASE_URL}/vm_console?vm=vm-00010#token={console.token}")
```

`console.mode` is `tty` for the serial console and `ssh` for the SSH console. `console.expires_in` is the lifetime in seconds.

Open the printed URL in a browser. The page connects to the Atlas realtime service and sends the token one time. A second use of the token fails, because the realtime service deletes the token when it opens the session.
