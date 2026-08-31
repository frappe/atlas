# OpenResty

OpenResty accepts public HTTP and HTTPS traffic. It stores the live route maps and writes them to disk.

The controller must use the control daemon. Do not call the OpenResty admin API from a network client.

## Traffic paths

| Traffic | Route path | TLS certificate |
| --- | --- | --- |
| Site HTTP on port `80` | OpenResty sends HTTP to the site VM on port `80`. | Not used. |
| Site HTTPS on port `443` | OpenResty terminates TLS and sends HTTP to the site VM on port `80`. | The proxy wildcard certificate. |
| Custom-domain HTTP on port `80` | OpenResty sends HTTP to the site VM on port `80`. | Not used. |
| Custom-domain HTTPS on port `443` | OpenResty reads SNI and sends TLS to the site VM on port `443`. | The certificate on the site VM. |

SNI means Server Name Indication. The client sends the requested TLS host name in the TLS start message.

OpenResty drops an HTTPS connection with no SNI. It uses the placeholder certificate and an error page for an unknown custom domain.

## Site traffic

The region file stores the root domain without the wildcard mark.

```text
/var/lib/nginx/region
iad.frappe.dev
```

The wildcard certificate must cover `*.iad.frappe.dev`. OpenResty uses this certificate for site HTTPS traffic.

The site map uses the first host label as its key. For example, `erp.iad.frappe.dev` uses the `erp` key.

## Custom-domain traffic

OpenResty does not decrypt custom-domain TLS traffic. The site VM must listen on port `443` and must have the certificate for its custom domain.

The HTTP worker and stream worker use separate maps. A custom-domain update goes through the private SNI bridge so both maps get the same data.

## Route maps

OpenResty stores site and custom-domain maps in shared memory. A map change takes effect without a full OpenResty reload.

OpenResty writes the maps below `/var/lib/nginx` after a short delay.

| File | Data |
| --- | --- |
| `map.json` | Site map. |
| `domains-http-map.json` | Custom-domain HTTP map. |
| `sni-map.json` | Custom-domain TLS map. |

OpenResty loads these files when it starts. The controller remains the source of truth and must send the full maps when it restores a VM.

## Certificates

The setup script creates a placeholder certificate in `/var/lib/nginx/certs/_placeholder`. This certificate lets OpenResty start before the controller sends the region certificate.

The control daemon writes the active wildcard certificate below `/var/lib/nginx/certs/<region>`. The daemon then sets the active links at `/var/lib/nginx/certs/fullchain.pem` and `/var/lib/nginx/certs/privkey.pem`.

OpenResty uses the placeholder certificate for its unconfigured custom-domain server. Do not remove the placeholder certificate files.

## Private interfaces

| Path | Use |
| --- | --- |
| `/run/nginx/admin.sock` | Private HTTP API for the control daemon. |
| `/run/nginx/sni-bridge.sock` | Private map bridge between HTTP and stream workers. |
| `/var/lib/nginx/acme` | ACME challenge files for the region wildcard certificate. |

Do not expose either socket outside the VM.

## Service checks

```sh
sudo systemctl status openresty.service
sudo /usr/local/openresty/nginx/sbin/nginx -t -c /etc/nginx/nginx.conf
journalctl -u openresty.service -f
```
