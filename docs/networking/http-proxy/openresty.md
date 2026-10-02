# OpenResty data plane

OpenResty accepts public HTTP and HTTPS traffic on each proxy node. It keeps the live route maps in shared memory. The control daemon is the only supported map writer.

## Traffic paths

| Traffic | Route path | TLS certificate |
| --- | --- | --- |
| Site HTTP on port `80` | Send HTTP to the site VM on port `80`. | Not used. |
| Auto proxy HTTP or HTTPS | Compute the VM address from the host and send HTTP to port `80`. | Regional wildcard certificate for HTTPS. |
| Site HTTPS on port `443` | Terminate TLS and send HTTP to the site VM on port `80`. | Regional wildcard certificate. |
| Custom-domain HTTP on port `80` | Send HTTP to the site VM on port `80`. | Not used. |
| Custom-domain HTTPS on port `443` | Read SNI and send TLS to the site VM on port `443`. | Certificate on the site VM. |
| Regional or node control HTTPS | Terminate TLS and send HTTP to `127.0.0.1:9000`. | Regional wildcard certificate. |

SNI means Server Name Indication. OpenResty drops an HTTPS connection with no SNI. It uses the placeholder certificate and an error page for an unknown custom domain.

## Control names

The apply command writes the first labels from `control.domain` and `control.node_domain` to `/var/lib/nginx/control-subdomain`. OpenResty routes these labels to the daemon before it reads the site map. In a normal Atlas configuration, these labels are `proxy` and the local `proxy-NNN` name.

The control daemon rejects `proxy` and every `proxy-*` site key. This rule keeps regional and node control traffic separate from tenant site routes.

## Site traffic

`/var/lib/nginx/region` stores the wildcard zone without `*.`. A host exactly one label below this zone uses the site map. For example, `erp.par-1.example.com` reads the `erp` key.

The wildcard certificate covers site HTTPS traffic. An address of `-` returns `503` for that site.

## Auto proxy traffic

An auto-proxy name sends HTTP to a VM without a saved site-map entry. The hostname uses a configured prefix, such as `site-` or `*-vm-`, followed by a base-36 label. OpenResty sends the request to the decoded VM mesh address on port `80`.

Atlas puts the 16-bit VM number above the 32-bit tenant ID: `value = (vm << 32) | tenant`. The VM number is the numeric suffix of its Atlas ID, so `vm-2-0011` uses `11`. Atlas writes the 48-bit value in lowercase base 36, in at most 10 characters. The regional mesh prefix supplies `fdaa:<region>` when OpenResty decodes the label.

::: details Try the base-36 mapping in Python

This example creates a name and gets both IDs and the mesh address back from its label:

```python
from ipaddress import IPv6Address

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

def base36(number):
    result = ""
    while number:
        number, digit = divmod(number, 36)
        result = DIGITS[digit] + result
    return result or "0"

region, tenant, vm = 1, 2, 11
label = base36((vm << 32) | tenant)
name = f"site-{label}.example.com"

decoded_vm, decoded_tenant = divmod(int(label, 36), 1 << 32)
mesh = IPv6Address(
    (0xFDAA << 112) | (region << 96) | (decoded_tenant << 64) | decoded_vm
)
print(name, mesh)  # site-lpc8lqa.example.com fdaa:1:0:2::b
```

:::

The proxy accepts only configured host prefixes and a canonical lowercase label. The label has at most 10 digits and no leading zero. Values larger than 48 bits and unsupported prefixes do not route. The control daemon reserves matching site keys so a stored route cannot override an auto-proxy name.

::: info Static route, live destination
The name encodes tenant and VM identity, not a host location. WG Mesh finds the VM's current Metal host after a move. An auto-proxy name alone does not grant access through tenant or guest firewall rules.
:::

`/var/lib/nginx/auto-proxy` holds the address prefix and the configured host prefixes. An empty file disables static routing.

## Custom-domain traffic

OpenResty does not decrypt custom-domain TLS traffic. It connects to the site VM on port `443` and sends a PROXY protocol v2 header before the TLS data. The site VM must accept PROXY protocol v2 and hold the certificate for the requested domain.

OpenResty sends an HTTP-01 challenge for a custom domain to the site VM on port `80` with the requested host. A challenge for a name in the wildcard zone stays local and reads `/var/lib/nginx/acme`.

The HTTP and stream workers use separate maps. A custom-domain change passes through the private SNI bridge so both workers receive the same state.

## Route state

OpenResty writes its live maps below `/var/lib/nginx` after a short delay:

| File | Data |
| --- | --- |
| `map.json` | Site map. |
| `domains-http-map.json` | Custom-domain HTTP map. |
| `sni-map.json` | Custom-domain TLS map. |
| `auto-proxy` | Address and host prefixes for static routes. The control daemon writes it. |

The control daemon also stores the complete cluster snapshot in `/var/lib/nginx/cluster-state.json`. This snapshot contains both maps and their generation. At daemon startup, the control daemon installs its saved snapshot into OpenResty. It then replaces it with a newer peer snapshot when one is available.

Do not edit these files while the services run.

## Certificates

The setup script creates `/var/lib/nginx/certs/_placeholder`. This certificate lets OpenResty start before Atlas sends the regional certificate.

The apply command validates and installs the active wildcard certificate below `/var/lib/nginx/certs/<region>`. It then updates `/var/lib/nginx/certs/fullchain.pem` and `/var/lib/nginx/certs/privkey.pem`.

## Private interfaces

| Path | Use |
| --- | --- |
| `/run/nginx/admin.sock` | Map API for the local control daemon. |
| `/run/nginx/sni-bridge.sock` | Map bridge between HTTP and stream workers. The owner is `root:nginx`. |
| `/var/lib/nginx/acme` | ACME challenge files for the wildcard certificate. |

Do not expose either socket outside the VM.

## Checks

```sh
sudo systemctl status openresty.service
sudo /usr/local/openresty/nginx/sbin/nginx -t -c /etc/nginx/nginx.conf
journalctl -u openresty.service -f
```
