# Debug in production

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

Use debug commands to inspect routes and packet decisions. Keep event readers short under load; see [benchmark results](benchmark.md#debug-mode) for their CPU cost.

## Commands

```sh
atlas-wg-mesh debug status
atlas-wg-mesh debug enable
atlas-wg-mesh debug dump [--src ADDRESS] [--dst ADDRESS] [--tenant ID] [--action accept|drop|redirect]
atlas-wg-mesh debug top [--src ADDRESS] [--dst ADDRESS] [--tenant ID]
atlas-wg-mesh debug inspect --address ADDRESS
atlas-wg-mesh debug disable
```

Start a debugging session with `debug enable`, run `dump` or `top`, stop it with `Ctrl-C`, then run `debug disable`.

```sh
# Terminal 1
atlas-wg-mesh debug enable
atlas-wg-mesh debug dump --tenant 7

# After Ctrl-C
atlas-wg-mesh debug disable
```

## Status

```sh
atlas-wg-mesh debug status
```

Counters are cumulative for the lifetime of the pinned maps. `events lost` means the ring buffer filled before a reader consumed every event. Forwarding continues, but the trace is incomplete.

## `debug dump`

`dump` prints packet decisions and NDP messages. `--tenant` filters every event. `--src`, `--dst`, and `--action` filter packet events only.

### Cold remote cache

The first packet continues to Linux routing, which triggers NDP. The NDP hook learns the owner from the advertisement, and the guest retry is tunneled:

```text
VM        ACCEPT   src=fdaa:1:0:7::10 dst=fdaa:1:0:7::20 tenant=7
NDP       RX LEARN vm=fdaa:1:0:7::20 host=fdab::2 tenant=7
VM        REDIRECT src=fdaa:1:0:7::10 dst=fdaa:1:0:7::20 tenant=7
WIREGUARD ACCEPT   src=fdaa:1:0:7::20 dst=fdaa:1:0:7::10 tenant=7
VM        ACCEPT   src=fdaa:1:0:7::10 dst=fe80::1 tenant=7
```

The owner also logs `NDP TX ANNOUNCE` with the same VM and host pair. The last line is the guest reaching its gateway. `fe80::1` is outside `fdaa::/16`, so Linux handles it normally.

### Warm remote cache

Once the owner is known, there is no discovery:

```text
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
```

`REDIRECT` means the VM packet was encapsulated and sent through WireGuard. A matching `WIREGUARD ACCEPT` on the reply path confirms delivery from the remote host.

### Same host

VMs on the same host are not tunneled:

```text
VM        ACCEPT   src=fdaa:1:0:2::10 dst=fdaa:1:0:2::11 tenant=2
VM        ACCEPT   src=fdaa:1:0:2::10 dst=fdaa:1:0:2::11 tenant=2
VM        ACCEPT   src=fdaa:1:0:2::10 dst=fdaa:1:0:2::11 tenant=2
```

`ACCEPT` returns the packet to normal Linux routing.

### Policy refusals

Filter to refused VM traffic:

```sh
atlas-wg-mesh debug dump --action drop
```

```text
VM        DROP     src=fdaa:1:0:2::10 dst=fdaa:1:0:7::20 tenant=2
VM        DROP     src=fdaa:1:0:2::10 dst=fdab::2 tenant=2
VM        DROP     src=fdaa:1:0:2::99 dst=fdaa:1:0:2::20 tenant=2
```

These lines show, in order: tenant isolation, the host-underlay guard, and source ownership. Ownership is per interface, so a forged source registered to a different VM on the same host is refused in the same way.

### Stale location recovery

When a VM moves from `fdab::2` to `fdab::3`, the managed neighbour entry on each sender keeps probing, and the first advertisement from the new owner refreshes the location:

```text
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
WIREGUARD DROP     src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
NDP       RX LEARN vm=fdaa:1:0:2::20 host=fdab::3 tenant=2
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
WIREGUARD ACCEPT   src=fdaa:1:0:2::20 dst=fdaa:1:0:2::10 tenant=2
```

The old host drops the tunnel for a VM it no longer owns. Linux NUD probing of the managed neighbour entry produces the neighbour solicitation, and the new owner answers it with the Atlas option.

### Unreachable owner

Repeated `REDIRECT` events with no matching `WIREGUARD ACCEPT` indicate an unreachable cached owner:

```text
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
VM        REDIRECT src=fdaa:1:0:2::10 dst=fdaa:1:0:2::20 tenant=2
```

Purge every cached VM location for that host:

```sh
atlas-wg-mesh remote purge --host fdab::2
# removed 2 remote entries
```

The next guest packet triggers NDP again. A recovered host must remove registrations for VMs it no longer owns before it rejoins.

## `debug top`

`top` groups packet decisions by source and destination for the current reader session:

```text
src                 dst                 accept  drop  redirect
fdaa:1:0:2::10      fdaa:1:0:2::30      0       0     6
fdaa:1:0:2::30      fdaa:1:0:2::10      6       0     0
fdaa:1:0:2::10      fe80::1             1       0     0
```

A tunneled flow appears as two rows: `redirect` counts guest packets leaving, while `accept` counts replies arriving.

`top` redraws every five seconds, including while traffic is continuous. Use `debug dump` when you need individual packet decisions.

## `debug inspect`

```sh
atlas-wg-mesh debug inspect --address fdaa:1:0:2::20
```

Examples:

```text
VM: fdaa:1:0:2::11
local: true

VM: fdaa:1:0:2::20
local: false
remote host: fdab::2
route: fdab::2 from :: dev wg0 src fdab::1 metric 1024 pref medium
WireGuard peer: public key=FiytAIKu6c5dG3lnmlbZEoZCBss/POBd4Dx7WM3M53M= endpoint=172.16.8.2:51820 handshake=1788091224

VM: fdaa:1:0:9::99
local: false
remote: not learned
```

`not learned` is normal until a local guest contacts that VM. It is also expected after a remote purge.
