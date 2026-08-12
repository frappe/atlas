# Garage — distributed object storage on Atlas VMs

Atlas can provision ordinary Virtual Machines as [Garage](https://garagehq.deuxfleurs.fr/) nodes. A Garage deployment consists of one or more **data nodes** and optional **gateway nodes**, with Atlas responsible for building the node image, injecting the runtime configuration, joining nodes to the Garage cluster, and applying the cluster layout.

The important split is:

* **The image contains software, not credentials.** The Garage binary and systemd unit are baked into the image.
* **Atlas owns cluster configuration.** `Garage Settings` contains the cluster-wide secrets, domains, node count, and node membership.
* **Each VM owns its node identity.** After Garage starts, Atlas obtains the node's peer ID and stores it on the VM.
* **Gateway nodes expose S3.** Data nodes bind their S3 endpoints to loopback; gateway nodes bind them publicly and receive an nginx configuration which proxies the API and web endpoints.
* **The cluster is configured incrementally.** A VM is marked as a Garage node, configured while running, assigned a Garage layout position, and added to the corresponding `Garage Settings` machine table.

> **Current limitation.** The patch explicitly warns not to provision multiple Garage nodes concurrently because node configuration and cluster membership can race. Provisioning should therefore be performed sequentially until that lifecycle is made concurrency-safe.

---

## Why Garage

Atlas normally provisions VMs as independent compute instances. Garage adds a distributed object-storage role where several Atlas VMs cooperate as a single storage cluster.

A Garage VM has one of two roles:

* **`data`** — stores Garage metadata and object data and participates in the replicated storage layer.
* **`gateway`** — provides externally reachable S3 API/web endpoints and participates in the Garage cluster as a gateway.

The deployment is therefore represented centrally in Atlas rather than by treating each Garage VM as an unrelated service.

The cluster-wide configuration lives in the singleton `Garage Settings` DocType. Individual VMs carry only the information specific to that node, notably their Garage type and discovered peer ID.

## The shape

A Garage deployment looks roughly like this:

```text
                         Atlas / Frappe
                               │
                     Garage Settings
                     ┌─────────┴─────────┐
                     │                   │
               Data Machines       Gateway Machines
                     │                   │
             ┌───────┴───────┐     ┌─────┴─────┐
             │               │     │           │
          Garage VM       Garage VM Garage VM  ...
             │               │     │
          data node       data node gateway
             │               │     │
             └───────┬───────┘     │
                     │              │
                Garage cluster      │
                                    │
                             nginx :80
                              │       │
                         API domain  Web domain
                              │       │
                         Garage :3900 :3902
```

Atlas does not run Garage itself. It connects to the guest VM over SSH and installs the generated configuration into `/etc/garage.toml`.

## Image build

The Garage image is built through the ordinary Atlas image-build mechanism using the `garage` recipe.

The image build is intentionally limited to static software and service configuration:

* Garage `v2.3.0` by default.
* Architecture-specific Garage binary for:

  * `x86_64` / `amd64`
  * `aarch64` / `arm64`
* `ca-certificates`
* `curl`
* `nginx`
* `/etc/systemd/system/garage.service`

The build script obtains the Garage release directly from the Garage release server:

```sh
GARAGE_VERSION="${GARAGE_VERSION:-v2.3.0}"
```

and installs the binary as:

```text
/usr/local/bin/garage
```

The service is installed but **not started by the image build**.

This is deliberate: the image does not contain the cluster's RPC secret, admin token, metrics token, peer list, domains, or node-specific public address. Those values are generated or supplied by Atlas when the VM is configured.

For gateway images, the default nginx site is also removed:

```sh
rm -rf /etc/nginx/sites-enabled/default
```

The actual Garage-specific nginx configuration is written later by Atlas.

## Garage service

The image provides a simple systemd unit:

```ini
[Unit]
Description=Garage Data Store
After=network-online.target
Wants=network-online.target

[Service]
Environment='RUST_LOG=garage=info' 'RUST_BACKTRACE=1'
ExecStart=/usr/local/bin/garage server
LimitNOFILE=42000

[Install]
WantedBy=multi-user.target
```

The service is enabled and started by `configure_garage()` only after Atlas has written `/etc/garage.toml`.

This keeps the service lifecycle separate from image creation: cloning or provisioning an image does not immediately start an unconfigured Garage node.

---

## `Garage Settings`

`Garage Settings` is a singleton DocType containing the cluster-wide configuration.

| Field              | Purpose                                                   |
| ------------------ | --------------------------------------------------------- |
| `num_nodes`        | Number of required data nodes / Garage replication factor |
| `rpc_secret`       | Shared Garage RPC secret                                  |
| `admin_secret`     | Garage admin API token                                    |
| `metrics_secret`   | Garage metrics authentication token                       |
| `api_domain`       | S3 API domain                                             |
| `web_domain`       | S3 web domain                                             |
| `data_machines`    | Table of configured data-node VMs                         |
| `gateway_machines` | Table of configured gateway VMs                           |

The machine tables use the `Garage Virtual Machines` child table.

That child table contains:

| Field             | Purpose                               |
| ----------------- | ------------------------------------- |
| `virtual_machine` | Link to the Atlas `Virtual Machine`   |
| `peer_id`         | Fetched from the VM's `peer_id` field |

The machine lists are therefore Atlas's record of which VMs currently belong to each Garage role.

### Garage Settings actions

The Desk form exposes two cluster-level actions:

* **Apply Garage Layout**
* **Reconfigure all garages**

`Apply Garage Layout` connects to the first configured data node and looks for a staged Garage layout command in:

```sh
garage layout show
```

It extracts the generated:

```sh
garage layout apply --version ...
```

command and executes it.

If no staged layout changes are present, the operation reports:

```text
No staged layout changes found
```

`Reconfigure all garages` calls `configure_garage()` for every VM in both the data and gateway machine tables and then applies the Garage layout.

---

## VM integration

Garage is represented directly on the `Virtual Machine` DocType.

A VM can now carry:

| Field               | Notes                                                          |
| ------------------- | -------------------------------------------------------------- |
| `is_garage`         | Marks the VM as a Garage instance                              |
| `garage_type`       | `gateway` or `data`; set only once                             |
| `garage_configured` | Read-only flag indicating successful configuration             |
| `peer_id`           | Read-only Garage node identity returned by `garage node id -q` |

A Garage VM must therefore be explicitly marked before Atlas will configure it.

When a VM is:

```text
Running
```

and:

```text
is_garage = true
```

the VM form exposes:

```text
Configure Garage
```

The action invokes the whitelisted `configure_garage()` controller method and reloads the VM after completion.

---

## Configuration flow

A Garage node is configured with:

```text
Virtual Machine
      │
      │ is_garage = true
      │ garage_type = data|gateway
      ▼
Configure Garage
      │
      ▼
atlas.garage.configure_garage()
      │
      ├── validate VM
      ├── load Garage Settings
      ├── collect existing peer IDs
      ├── generate garage.toml
      ├── write /etc/garage.toml
      ├── start Garage
      ├── assign Garage layout position
      ├── obtain node peer ID
      ├── store VM.peer_id
      ├── mark garage_configured
      └── add VM to Garage Settings
```

The VM must be `Running`. Atlas rejects configuration otherwise.

The configuration is performed through the existing guest SSH machinery:

```python
connection_for_guest(vm)
```

and `_write_guest_file()`.

The generated configuration is written with mode:

```text
0600
```

so the Garage configuration containing cluster credentials is not world-readable.

---

## Generated `garage.toml`

Atlas generates the complete Garage configuration rather than relying on a pre-existing configuration inside the image.

The common configuration includes:

```toml
replication_factor = <num_nodes>
consistency_mode = "consistent"

metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
metadata_snapshots_dir = "/var/lib/garage/snapshots"

metadata_fsync = true
data_fsync = false

disable_scrub = false
use_local_tz = false
metadata_auto_snapshot_interval = "6h"

db_engine = "lmdb"

block_size = "1M"
block_ram_buffer_max = "256MiB"
block_max_concurrent_reads = 16
block_max_concurrent_writes_per_request = 10
lmdb_map_size = "1T"

compression_level = 1
```

Atlas also configures the cluster RPC endpoint:

```toml
rpc_secret = "<rpc-secret>"
rpc_bind_addr = "[::]:3901"
rpc_bind_outgoing = false
rpc_public_addr = "[<vm-ipv6>]:3901"
```

The VM's public IPv6 is therefore used as its Garage RPC address.

The generated configuration also explicitly disables world-readable secrets:

```toml
allow_world_readable_secrets = false
```

---

## Bootstrap peers

Garage nodes need to know how to find the existing cluster.

Atlas constructs the bootstrap peer list from the already configured data and gateway machines:

```python
bootstrap_peers = [
    row.peer_id
    for row in (garage.gateway_machines + garage.data_machines)
    if row.peer_id and row.peer_id != vm.peer_id
]
```

The current VM's own peer ID is excluded.

If peers exist, Atlas writes:

```toml
bootstrap_peers = [
    "<peer-1>",
    "<peer-2>",
    ...
]
```

If there are no existing peers, the `bootstrap_peers` configuration is omitted.

This makes the first node special: it can be configured without any existing Garage peer, while subsequent nodes receive the peer IDs already known to Atlas.

---

## Node identity

The Garage peer ID is not predetermined by Atlas.

After the service starts, Atlas waits for Garage to become responsive:

```sh
until garage status >/dev/null 2>&1; do
    sleep 2
done
```

It then assigns the node to a Garage layout:

```sh
garage layout assign \
    -z <server> \
    <capacity-or-gateway> \
    "$(garage node id -q | cut -d@ -f1)"
```

The node's identity is separately obtained with:

```sh
garage node id -q
```

Atlas stores the result in:

```text
Virtual Machine.peer_id
```

and uses that value when configuring future Garage nodes.

The same value is exposed through the `Garage Virtual Machines` child table via its `fetch_from` field.

---

## Data nodes vs gateway nodes

The `garage_type` field selects one of two configuration paths.

### Data nodes

A data node receives:

```toml
[s3_api]
api_bind_addr = "127.0.0.1:3900"
s3_region = "<region>"
root_domain = ".<api-domain>"

[s3_web]
bind_addr = "127.0.0.1:3902"
root_domain = ".<web-domain>"
add_host_to_metrics = true
```

The S3 API and web interfaces therefore listen only on loopback.

The node is assigned storage capacity based on the free space in:

```text
/var/lib/garage/data
```

using:

```sh
df -B1 --output=avail /var/lib/garage/data
```

The server name is also passed to the Garage layout as its zone:

```sh
-z <vm.server>
```

### Gateway nodes

A gateway receives:

```toml
[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "<region>"
root_domain = ".<api-domain>"

[s3_web]
bind_addr = "[::]:3902"
root_domain = ".<web-domain>"
add_host_to_metrics = true
```

and is assigned with:

```sh
--gateway
```

Unlike data nodes, the gateway's Garage S3 endpoints are not restricted to loopback.

Atlas also writes an nginx configuration for the gateway.

---

## Gateway nginx

Gateway VMs receive:

```text
/etc/nginx/conf.d/garage.conf
```

The configuration defines two virtual hosts:

```text
<api-domain> → 127.0.0.1:3900
<web-domain> → 127.0.0.1:3902
```

The proxy forwards the original host and client address:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
```

The gateway therefore has this shape:

```text
                  IPv4 / IPv6
                       │
                       ▼
                 nginx :80
                  /         \
                 /           \
        API domain           Web domain
             │                    │
             ▼                    ▼
       127.0.0.1:3900      127.0.0.1:3902
             │                    │
             └────────┬───────────┘
                      ▼
                   Garage
```

The nginx configuration is only written for `gateway` nodes.

Atlas starts both services for a gateway:

```sh
systemctl enable --now garage.service nginx.service
```

while data nodes only start:

```sh
systemctl enable --now garage.service
```

---

## Cluster-wide admin and metrics endpoints

The generated Garage configuration enables the admin API on:

```toml
[admin]
api_bind_addr = "0.0.0.0:3903"
metrics_token = "<metrics-secret>"
metrics_require_token = true
admin_token = "<admin-secret>"
```

The metrics endpoint therefore requires the configured metrics token, while the admin API receives the configured admin token.

These credentials originate from `Garage Settings` and are injected when each VM is configured.

The image itself never contains these credentials.

---

## Secrets and custody

Garage credentials are cluster-wide values stored in `Garage Settings`:

* `rpc_secret`
* `admin_secret`
* `metrics_secret`

Atlas passes them into `_generate_garage_config()` and writes the resulting configuration to:

```text
/etc/garage.toml
```

with mode `0600`.

The image build does not contain any of these values.

This gives the image a reusable role:

```text
Promoted Garage image
        │
        ├── Garage binary
        ├── systemd service
        └── nginx
              │
              ▼
       VM-specific configuration
              │
              ├── RPC secret
              ├── admin token
              ├── metrics token
              ├── peer list
              ├── node IPv6
              ├── node role
              └── domains
```

The result is that cloning the image does not clone a pre-existing Garage cluster identity or cluster credentials.

---

## Layout lifecycle

Garage layout management is separate from initial node configuration.

When a node is configured, Atlas executes:

```sh
garage layout assign ...
```

to stage the node's position in the cluster.

The `Garage Settings` **Apply Garage Layout** action then connects to the first data node and extracts the staged application command from:

```sh
garage layout show
```

If Garage reports a command such as:

```sh
garage layout apply --version ...
```

Atlas executes that command.

This means node assignment and layout application are intentionally two separate operations:

```text
Configure node
      │
      └── garage layout assign
                 │
                 ▼
           staged layout
                 │
                 ▼
       Apply Garage Layout
                 │
                 └── garage layout apply
```

The cluster layout can therefore be staged as nodes are added and explicitly applied from the Garage Settings page.

---

## Reconfiguration

`Garage Settings` also provides **Reconfigure all garages**.

The operation walks:

1. every VM in `data_machines`;
2. every VM in `gateway_machines`;

and calls:

```python
configure_garage(i.virtual_machine)
```

for each.

After all nodes have been configured, Atlas calls:

```python
self.apply_layout()
```

This provides a coarse-grained way to regenerate the configuration of the entire Garage fleet after cluster-level settings change.

The same mechanism also reconstructs the bootstrap peer list from the currently recorded node identities.

---

## Error handling

`configure_garage()` checks several prerequisites before changing the VM:

* The VM must have `is_garage` set.
* The VM must be `Running`.
* Garage's SSH configuration must succeed.
* The Garage configuration/start/layout command must exit successfully.
* The Garage node ID command must exit successfully.

Guest command failures are recorded through Atlas's guest-task mechanism:

```python
_record_guest_task(...)
```

The configuration operation then raises an error containing the command's exit status and the final portion of stderr.

For example, a failed configuration reports:

```text
Configuring garage on <vm> failed (exit <code>): <stderr>
```

A failure to obtain the peer ID is reported separately.

Only after both commands succeed does Atlas set:

```text
garage_configured = 1
```

and persist the node's `peer_id`.

---

## Node registration in `Garage Settings`

After successful configuration, Atlas determines the relevant machine table from the VM's role:

```python
table = f"{vm.garage_type}_machines"
```

It then checks whether the VM is already present.

If not, it appends:

```text
virtual_machine = <vm>
```

to the corresponding table and saves `Garage Settings`.

This makes configuration idempotent at the Atlas membership level: reconfiguring an already registered VM does not append another copy of the same VM.

---

## Build and provisioning workflow

The intended operational flow is:

```text
1. Build Garage image
        │
        ▼
2. Promote/use Garage image
        │
        ▼
3. Provision ordinary Atlas VM
        │
        ▼
4. Mark VM:
       is_garage = true
       garage_type = data|gateway
        │
        ▼
5. Start VM
        │
        ▼
6. Configure Garage
        │
        ├── write garage.toml
        ├── start Garage
        ├── discover peer ID
        ├── assign layout
        └── register VM
        │
        ▼
7. Repeat for additional nodes
        │
        ▼
8. Apply Garage Layout
```

For a gateway node, step 6 additionally writes nginx configuration and starts nginx.

The patch's own operational warning is important here:

> Do not provision multiple nodes concurrently.

The current implementation does not provide a locking or transactional mechanism around cluster membership, peer discovery, or layout assignment.

---

## What is stored where

### Image

Stored in the Garage image:

* Garage binary
* Garage systemd unit
* nginx package
* nginx default-site removal

Not stored in the image:

* RPC secret
* admin token
* metrics token
* cluster peer list
* node-specific RPC address
* API domain
* web domain

### `Garage Settings`

Stores:

* cluster secrets
* data-node count
* API domain
* web domain
* data-node membership
* gateway-node membership

### `Virtual Machine`

Stores:

* whether the VM is a Garage node
* Garage role
* whether configuration completed
* Garage peer ID

### Guest VM

Stores:

```text
/etc/garage.toml
/etc/nginx/conf.d/garage.conf   # gateway only
/usr/local/bin/garage
/etc/systemd/system/garage.service
```

The Garage configuration contains the runtime credentials and is written with restrictive permissions.

---

## Not in this iteration

* **No concurrent node provisioning.** The patch explicitly warns against provisioning multiple Garage nodes at the same time because of a possible race.
* **No automatic cluster-wide locking.** There is no lock around peer discovery, machine-table updates, or layout assignment.
* **No automatic layout application after every node.** Node assignment stages layout state; applying it is a separate Garage Settings action.
* **No automated removal/decommissioning flow.** The patch adds configuration and layout application, but does not add a corresponding Garage-node removal lifecycle.
* **No automated TLS configuration for nginx.** The generated gateway nginx configuration listens on HTTP port `80`; certificate management is not part of this patch.
* **No dedicated Garage VM type.** Garage nodes remain ordinary Atlas Virtual Machines with `is_garage` and `garage_type` metadata.
* **No separate image for data and gateway nodes.** The same Garage image can be used for either role; the role-specific behavior is injected at configuration time.
* **No credentials baked into the image.** Runtime Garage credentials remain configuration supplied by Atlas.

