# High availability

The HTTP proxy cluster has 1 to 5 nodes in one region. Each node serves traffic and keeps a local route map.

Atlas manages the nodes, Domain Name System (DNS) records, peer membership, certificates, and cluster passwords. See the [control daemon guide](control-daemon.md) for the public API.

## Addresses

`proxy.<wildcard-domain>` is the regional control address. Route 53 creates one multivalue A record and one HTTPS `/healthz` health check for each active node. The TTL is 120 seconds.

`proxy-NNN.<wildcard-domain>` is the stable address of one node. Its A record has a 3600-second TTL. Peers use these node addresses for HTTPS requests.

`*.<wildcard-domain>` is a CNAME to `proxy.<wildcard-domain>`. Its TTL is 3600 seconds. This record sends site names to the regional proxy addresses.

Route 53 normally removes an unhealthy node from its answers. If all nodes are unhealthy, Route 53 returns all records. See [Multivalue answer routing](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-multivalue.html).

## State

Each node stores the site map, domain map, election term, last vote, last operation ID, and generation. The file is `/var/lib/nginx/cluster-state.json`.

The generation increases for each mutation. The operation ID identifies a mutation when 2 snapshots have the same generation.

## Leader election

A follower starts an election after its leader heartbeats stop. The election timeout is a random value from 300 ms through 450 ms.

A candidate needs a majority of votes. Each voter compares the candidate mutation term and generation with its local values.

The leader sends a heartbeat every 100 ms. It becomes a follower if a heartbeat response shows a higher term.

## Write path

Any ready node accepts a public mutation. A follower forwards the mutation to the leader with a 1500 ms timeout, which covers one peer repair.

The leader applies the mutation locally and assigns the next generation. It sends the mutation to all other nodes at the same time.

Each peer request has a 200 ms timeout. The leader returns success only after it receives the required acknowledgements.

A rejected mutation keeps the leader status, such as `409`. A follower returns `503` when it cannot reach the leader, and `502` when the cluster password fails.

| Configured nodes | Required acknowledgements | Unavailable nodes allowed |
| ---------------- | ------------------------- | ------------------------- |
| 1                | 1                         | 0                         |
| 2                | 2                         | 0                         |
| 3                | 3                         | 0                         |
| 4                | 3                         | 1                         |
| 5                | 3                         | 2                         |

The acknowledgement count includes the leader. Thus, a 5-node cluster continues to accept writes while 2 nodes are unavailable.

A successful response can leave an unavailable node behind. A later heartbeat makes that node install the leader snapshot.

A `503` response can mean that some nodes applied the mutation. Send the same desired mutation again because all mutation types are idempotent.

## Node start and recovery

A node first loads its stored snapshot and applies it to OpenResty. It then asks all configured peers for their status.

The node selects the available peer with the highest generation. It downloads that snapshot when the peer generation is higher than its generation.

Atlas does not send route state to a new node.

The readiness route returns `503` until the node has synchronized and knows a leader. Route 53 uses this route for its health check.

## Authentication

Public API requests use the regional Proxy password or a JSON Web Token. Each node gets the merged regional key set from Atlas. It accepts Central and its regional Atlas issuer. The key namespace must match the issuer.

Peer requests use the regional proxy password in the `X-Atlas-Cluster-Password` header. Public requests use the same password as a Bearer credential.

Public and peer requests accept the previous password for 10 minutes after rotation. The previous password expires at the same time on all nodes.
