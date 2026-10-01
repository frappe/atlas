# How Atlas chooses a host

Atlas chooses a host for a new VM, a resize, or a move. The code calls this **placement**. It must never let two requests reserve the same memory or disk.

A **strategy** ranks hosts. The **capacity check** decides whether a host can take the VM. A strategy can change the order, never the capacity rule.

## How a host is chosen

1. **Host pool.** A host qualifies when it is `Running` and has finished setup. It must also match the image architecture and have a capacity sample less than 2 minutes old. With **dedicated sleepy hosts** enabled, [sleepy VMs](sleepy-vms.md) and regular VMs use separate pools.
2. **Fit.** Atlas subtracts work the sample cannot include yet: recent drafts, VMs created after the sample, and incoming migrations. **Memory and disk are hard limits.** CPU can be oversubscribed. It only affects ranking.
3. **Rank.** The selected strategy sorts the hosts that fit.
4. **Lock and recheck.** Atlas takes a MariaDB named lock for the top host, rechecks committed capacity under `READ COMMITTED`, commits the draft, and releases the lock.

A resize or migration first tries to stay on the current host, which then needs room only for the increase.

## Strategies

Choose the strategy in **Atlas Settings**. `balanced` is the default.

| Strategy | Goal | Regular VMs | Sleepy VMs |
| --- | --- | --- | --- |
| `balanced` | Spread risk and load | Rank by the keys below | Same keys, with sleepy memory discounted |
| `spread-3` | Keep hosts evenly used | Least used host first | Pack: most subscribed host first |
| `best-fit` | Keep whole hosts free for large VMs | Most used host first | Pack: most subscribed host first |

**`balanced`** sorts hosts by these keys, in order:

1. Fewest VMs from the same tenant, so one host failure hits fewer of a tenant's VMs.
2. Lowest placement rate in the last 5 minutes, so a burst spreads out.
3. Smallest CPU shortfall: requested CPU above the host's free CPU.
4. Lowest projected memory or disk use, whichever is higher.
5. Host name, as a tie-breaker.

For example, if host A already runs 2 of the tenant's VMs and host B runs 1, host B wins even if A is quieter. For a sleepy VM, `balanced` divides sleepy memory by `sleepy_vm_overcommit_factor` when it ranks, because sleepy VMs are often stopped. The capacity check still uses real memory.

**`spread-3`** ranks regular hosts by projected use, lowest first. A VM that would bring host A to 80% and host B to 60% goes to host B. It packs sleepy VMs onto the host with the most subscribed sleepy memory, so sleepy hosts fill one at a time.

**`best-fit`** ranks regular hosts by projected use, highest first: the same VM goes to host A. This packs VMs tightly and keeps other hosts empty for large requests. It packs sleepy VMs the same way as `spread-3`.

To add a strategy, subclass `PlacementStrategy` and register it. The [VM module specification](../../atlas/vm/SPEC.md#placement) shows how.

## Affinity rules

Affinity rules limit the hosts that can hold a VM. A rule reads the tags of a host (Metal Server), or the tags of the VMs that run on the host.

Atlas validates the rules in a create request and stores them in the `affinity_rules` field of the Virtual Machine record. Placement applies them when it creates the VM, when it migrates the VM to a host that it chooses, and when a resize moves the VM to another host. A resize that stays on the current host does not check them. A migration to a host that an operator names does not check them.

Only a System Manager can set rules, and only on a privileged VM. Atlas rejects other requests with `403`.

### Rule types

```text
affinity_rules = [node, ...]               every node must hold (AND); absent or [] = no rules
node           = rule | any_of | all_of
any_of         = {"any_of": [node, ...]}   at least one node holds (OR)
all_of         = {"all_of": [node, ...]}   every node holds (AND)
rule           = {"resource": resource, "operator": operator, "tags": {key: value, ...}, "within": key}
resource       = "metal_server" | "virtual_machine"
operator       = "has" | "has_not"
```

| Field | Values | Meaning |
| --- | --- | --- |
| `resource` | `metal_server`, `virtual_machine` | `metal_server` reads the tags of the host. `virtual_machine` reads the tags of each VM on the host. |
| `operator` | `has`, `has_not` | `has` needs a resource with every pair in `tags`. `has_not` is the opposite of `has`. |
| `tags` | 1 to 32 key and value pairs | Every pair must be on the same resource. Atlas trims each key and value. A key has 1 to 128 characters. A value has at most 1000 characters. |
| `within` | A host tag key, such as `rack`. Optional. | Only for `virtual_machine`. The rule reads the VMs on every host that has the same value for this key as the candidate host. Without `within`, it reads only the VMs on the candidate host. |
| `any_of` | 1 or more nodes | The group holds when at least one node holds. |
| `all_of` | 1 or more nodes | The group holds when every node holds. |

A rule asks for one of these conditions on a candidate host:

| `resource` | `operator` | The host matches when |
| --- | --- | --- |
| `metal_server` | `has` | The host has every pair. |
| `metal_server` | `has_not` | The host is missing at least one pair. |
| `virtual_machine` | `has` | At least one VM on the host has every pair. |
| `virtual_machine` | `has_not` | No VM on the host has every pair. |

For `virtual_machine`, one rule with two pairs needs one VM that has both pairs. An `all_of` group of two rules accepts two different VMs.

A `virtual_machine` rule counts only the VMs of the same tenant. It counts a VM on its host, also when the VM is a draft, and on the destination host of its active migration. It does not count a VM that is being terminated, or the VM that placement moves.

With `within`, the hosts that share the candidate host's value for the key are its **group**. For example, with `"within": "rack"` and a candidate host tagged `rack: a`, the group is every host tagged `rack: a`, whatever its status. `has` then needs a VM with every pair somewhere in the group, and `has_not` needs no such VM in the group. A candidate host without the key fails the rule for both operators, because Atlas cannot tell which group it is in.

### Examples

This create request tags a Cargo Server VM and asks for a host that has no other Cargo Server VM:

```json
{
  "tags": {"role": "cargo-server"},
  "affinity_rules": [
    {"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "cargo-server"}}
  ]
}
```

This request asks for a storage-optimised host in rack `a`, or for any memory-optimised host:

```json
{
  "affinity_rules": [
    {"any_of": [
      {"resource": "metal_server", "operator": "has", "tags": {"type": "storage-optimised", "rack": "a"}},
      {"resource": "metal_server", "operator": "has", "tags": {"type": "memory-optimised"}}
    ]}
  ]
}
```

This request keeps a database replica out of every rack that already has one:

```json
{
  "tags": {"role": "db-replica"},
  "affinity_rules": [
    {"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "db-replica"}, "within": "rack"}
  ]
}
```

If a replica runs on host 1 in rack `a`, host 3 in rack `a` fails the rule, although host 3 runs no replica itself. Hosts in rack `b` pass. A host without a `rack` tag fails.

### Validation

Atlas rejects the request with `400` when one of these is true:

- A rule has an unknown field, for example `weight`, or a rule has no `resource`, `operator`, or `tags`.
- A `metal_server` rule has `within`, or `within` is empty or longer than 128 characters.
- `resource` or `operator` has an unknown value.
- `tags` is empty, a tag breaks the limits above, or two keys are the same after Atlas trims them.
- An `any_of` or `all_of` group is empty, or a group object has more than one key.
- The request has more than 16 rules in total, including the rules in groups.
- Groups nest more than 5 deep.

### How placement applies the rules

The rules are a filter before the strategy. When at least one host that meets the rules has room, placement removes the other hosts from its host pool. Then the strategy ranks the remaining hosts as usual.

The host pool can be up to 1 second old, so two placements at the same time can see the same hosts. For this reason, Atlas checks the rules again while it holds the host lock, against the committed VMs on the host. If two Cargo Server VMs with `has_not {"role": "cargo-server"}` arrive together, the second one reads the draft of the first one. Placement then treats that host like a locked host and tries again with a fresh host pool, so the second VM goes to another host.

A rule with `within` reads the VMs on every host of the group. Thus, placement also locks each other host of the group before it checks the rule, and keeps those locks until the VM record commits. It does not wait for these locks, so two placements that need the same group cannot deadlock. If another placement holds one of them, placement treats the candidate host as locked and tries again.

The **Affinity Matching** setting in Atlas Settings, on the VM Scheduler tab, selects what happens when no host that meets the rules has room:

| Value | No host that meets the rules has room | Every host that meets the rules is locked |
| --- | --- | --- |
| Enforced (default) | The request fails with `affinity_unsatisfied` (`503`). Atlas does not add a host. | `placement_busy` |
| Preferred | Placement ignores the rules and uses every host. | `placement_busy` |

A locked host is not proof that no host meets the rules. Thus, Preferred matching does not ignore the rules when the hosts are only locked.

## Under contention

Concurrent requests rank hosts the same way, so they would all wait on the same top host. If another request holds that host's lock, Atlas tries the remaining hosts in random order.

It probes at most 16 hosts, tries up to 3 times, and gives up after **0.4 seconds** in total.

| Result | Meaning | Action |
| --- | --- | --- |
| Host selected | The draft or migration holds the capacity. | Atlas sends the host request. |
| `out_of_capacity` | No host in the pool fits. | Retry later. With auto-spawn enabled, Atlas queues a new host. |
| `placement_busy` | Hosts fit, but all were locked until the deadline. | Retry after `Retry-After` (1 second). |

Lock contention never creates a host. Only a full pool does.

## Limits and recovery

A healthy host drops out of the pool when its sample gets older than 2 minutes, for example after failed host syncs. An uncertain create keeps its capacity reserved until Metal confirms presence or absence. Check sample age and draft reservations before you change the strategy.

## Experimental

Offline and live simulators compare strategies outside the request path. Their results do not prove a strategy is safe for a region.

::: details Source code and tests

- [Placement context](../../atlas/vm/core/placement/context.py) loads samples, subtracts reservations, and rechecks capacity under the host lock.
- [Placement strategy base](../../atlas/vm/core/placement/strategies/base.py) ranks candidate hosts and defines capacity and busy results.
- [Affinity rules](../../atlas/vm/core/placement/affinity.py) parse, validate, and serialize the rule tree.
- [Transaction setup](../../atlas/vm/core/placement/transaction.py) enables READ COMMITTED.
- [VM creation](../../atlas/vm/core/vm_service.py) commits the draft after placement.
- [Placement tests](../../atlas/vm/core/placement/test_context.py) check capacity and lock behavior.
- [Affinity tests](../../atlas/vm/core/placement/test_affinity.py) check the rule shape and each rejected input.

:::
