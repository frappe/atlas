# SSHPiper - VM SSH ingress

SSHPiper runs in dedicated, operator-owned Atlas virtual machines. It is not a
service on every compute Server. Each gateway is built from the committed
[`sshpiper/`](../sshpiper) image recipe, has its own Reserved IPv4, and routes
public SSH connections to tenant VMs over their public IPv6 addresses.

```
ssh <vm-title>@<gateway-reserved-ip>
```

The SSH username is `Virtual Machine.title`. Atlas requires that title to resolve
to exactly one running, non-gateway VM; duplicate titles fail closed.

## The shape

- Build an `sshpiper` Image Build from stock Ubuntu and promote its snapshot.
- Deploy one or more ordinary VMs from that image and mark each `is_sshpiper`.
- Attach a dedicated Reserved IPv4 to every gateway.
- Run **Configure SSHPiper** on each gateway. Atlas installs the gateway token
  and the private key of the compute Server hosting that gateway, then starts
  `sshpiper.service`.
- Give each compute Server's gateway its own SSH DNS name. A gateway serves only
  tenant VMs on its own Server; do not round-robin gateways from different
  Servers under one name.

A gateway uses port 22 for public SSHPiper ingress. Its own management `sshd`
runs on port 222. Compute-host maintenance SSH also remains on port 222, but no
SSHPiper process runs on the compute host.

## Image and runtime state

The `sshpiper` Image Build uploads the committed tree and runs
[`sshpiper/build.sh`](../sshpiper/build.sh) inside a build VM. The build compiles
the Atlas Go plugin, installs the pinned sshpiperd release and systemd unit, and
moves guest management SSH to port 222. It bakes no credentials and leaves the
service disabled before snapshotting.

The image includes the `sshpiper.crypto` submodule. Initialize submodules before
building:

```
git submodule update --init --recursive
```

Configuration writes two root-only files into a deployed gateway:

- `/etc/atlas/sshpiper.env`: Atlas URL, gateway VM UUID, and per-gateway token.
- `/etc/atlas/server_id_ed25519`: the hosting compute Server's private key.
  `provision-vm.py` and `rebuild-vm.py` inject its public half only into guests
  on that Server. No Atlas-global private or public key is installed for
  SSHPiper.

The gateway token is stored encrypted in `Virtual Machine.sshpiper_api_key`.
`sshpiper_configured` records that runtime configuration completed.

## Lookup and authentication

For every downstream public-key attempt, the Go plugin calls:

```
GET /api/method/atlas.atlas.sshpiper.lookup_virtual_machine_ssh
X-Atlas-SSHPiper-Token: <gateway token>
```

Query arguments are `gateway=<gateway-vm-uuid>` and `vm_name=<ssh-username>`.
The method is guest-callable only because the daemon has no Frappe session; the
per-gateway token is mandatory and compared in constant time.

A successful response contains the target VM UUID, title, Server, IPv6 address,
and the non-empty lines from `Virtual Machine.ssh_public_key`. The plugin exposes
those lines as `AuthorizedKeys`. Only a matching downstream public key can
continue. It then connects to `[target-ipv6]:22` as root using that compute
Server's private key. Password authentication is never offered.

The API restricts lookup to `target.server == gateway.server`. A gateway
compromise therefore exposes the compute Server key and guests on that Server,
not a regional Atlas fleet key. Network policy should allow each gateway to
reach its Server's guest IPv6 port 22 while denying that port from untrusted
sources once per-VM ingress filtering is implemented.

## Operator workflow

1. Create an Image Build with recipe `sshpiper` and wait for `Available`.
2. Promote the snapshot to a Virtual Machine Image.
3. On each compute Server, provision a VM from that image, set `is_sshpiper`,
   and wait for `Running`.
4. Allocate and attach a dedicated Reserved IPv4 to that VM.
5. Open the VM and run **Configure SSHPiper**.
6. Point the public SSH DNS name at the Reserved IPv4.
7. Test with `ssh <vm-title>@<dns-name>` using a key stored on that target VM.

Roll a Server's gateways one at a time: deploy and configure a replacement on
the same Server, move that Server's SSH DNS record, drain the old address, then
terminate the old VM.

## Failure modes

- `Not permitted`: missing/wrong gateway token, duplicate target titles, target
  not Running, or a target marked as a gateway.
- `returned no public keys`: the target has no usable `ssh_public_key` lines.
- `too many colons in address`: an IPv6 address was not formatted with
  `net.JoinHostPort`; the plugin must return `[ipv6]:22`.
- `http2: frame too large`: plugin output corrupted the RPC stream; plugin code
  must not print arbitrary data to stdout or stderr.
- Gateway configuration cannot connect: the baked gateway manages itself on
  port 222, and the Reserved IPv4 must already be attached.
