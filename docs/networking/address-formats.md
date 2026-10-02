# Address formats

Atlas derives each private IPv6 address from IDs. No service looks up an address in a table. This page is the reference for each layout.

## Rules for every layout

Each field starts on a 16-bit boundary, so you can read the IDs from the address. The first hextet gives the address type, and the second hextet gives the region.

The tenant ID has 32 bits. A VM number and a WireGuard peer number have 16 bits each and use the last hextet.

Padding fields are zero. They hold space for later fields. Do not use them.

| Prefix | Use |
| --- | --- |
| `fdaa::/16` | VM mesh addresses and the Atlas address |
| `fdab::/16` | Host `wg0` addresses |
| `fdac::/16` | WireGuard gateway peers |

## VM mesh address

```text
fdaa : region : tenant (32 bits) : padding (48 bits) : VM number
```

Example: `vm-42-0007` in region `1` is `fdaa:1:0:2a::7`.

The VM name is `vm-<tenant ID>-<VM number>`. Each tenant has its own counter, so a VM number is unique only within its tenant. Each tenant has the `/64` `fdaa:<region>:<tenant>::/64`. A tenant can have VM numbers from 1 to 65,535. Atlas refuses a larger number.

## Atlas address

```text
fdaa : region : 0 : 0 : ffff : ffff : ffff : ffff
```

The Atlas address is in tenant 0 and has all padding bits set. No VM can have this address.

## Host wg0 address

```text
fdab : region : low 96 bits of the host UUID
```

Example: `fdab:1:7f90:766a:8d2c:2f76:8d76:2045`. This layout has no tenant or VM field.

## WireGuard gateway peer

```text
fdac : region : node : tenant (32 bits) : padding (32 bits) : peer number
```

Example: peer `9` of tenant `42` on `wireguard-002` in region `1` is `fdac:1:2:0:2a::9`.

The node field comes before the tenant. Then each node owns one `/48`, and a host can send the replies for that node to one VM. The node address is `fdac:<region>:<node>::1`. See [WireGuard gateway](wireguard-gateway.md).

## IPv6 router public address

```text
<provider block, /80 or shorter> : tenant (32 bits) : VM number
```

Example: block `2001:db8:1:2:3::/80` maps `fdaa:1:0:2a::7` to `2001:db8:1:2:3:0:2a:7`.

The public address has no padding. A `/80` block has exactly 48 host bits. A mesh address with padding has no public address. See [IPv6 router](ipv6-router.md).

## HTTP proxy auto-proxy label

```text
label = base36((VM number << 32) | tenant)
```

The value has 48 bits, so a label has at most 10 characters. See [OpenResty](http-proxy/openresty.md#auto-proxy-traffic).
