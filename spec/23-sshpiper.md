# SSHPiper — VM SSH ingress

SSHPiper is the public SSH entrypoint for virtual machines. Operators still
reach the host over Atlas' root SSH transport, but end users connect to a VM
through the host's port 22:

```
ssh <vm-title>@<server-public-name-or-address>
```

The SSH username is the `Virtual Machine.title`, not the VM document name. The
server that receives the connection asks Atlas which VM that title maps to on
this server, verifies the user's public key against Atlas' stored key list, then
opens the upstream SSH connection to the guest.

## Port ownership

Bootstrap moves the host's own `sshd` to port 222. Port 22 belongs to
SSHPiper.

| Port | Listener | Purpose |
| --- | --- | --- |
| `222` | host `sshd` | Atlas Task transport into the Server as root. |
| `22` | `sshpiper.service` | Public SSH ingress to Firecracker guests. |

Fresh cloud images still answer host SSH on port 22 before bootstrap has run.
After bootstrap, Atlas server maintenance traffic uses port 222; guest ingress
uses port 22.

## Lookup API

The host-side plugin calls a whitelisted Server-scoped method:

```
/api/method/atlas.atlas.doctype.server.server.lookup_virtual_machine_ssh
```

Required query arguments:

| Argument | Meaning |
| --- | --- |
| `server` | The `Server.name` UUID this host represents. |
| `vm_name` | The SSH username, resolved against `Virtual Machine.title`. |

Required header:

```
X-Atlas-Server-Token: <server token>
```

The method is `allow_guest=True` only so a host-side daemon can call it without
a Frappe session. It is still authenticated: the token is a hidden Password
field on the `Server` row (`sshpiper_api_key`), generated during bootstrap and
scoped to exactly that server. After token validation, the method returns data
only if the VM row has `server == <that server>`.

Response shape:

```json
{
  "message": {
    "virtual_machine": "d98a761c-09cf-46cf-98aa-afe6fdc521d7",
    "title": "vm1",
    "server": "e9a56051-6b39-448d-b6cb-4b1ee2182b39",
    "ipv6_address": "2400:6180:100:d0:0:1:58c9:a003",
    "host": "2400:6180:100:d0:0:1:58c9:a003",
    "public_keys": [
      "ssh-ed25519 AAAA... user@example"
    ]
  }
}
```

`public_keys` is derived from `Virtual Machine.ssh_public_key`, one non-empty,
non-comment line per key.

## Plugin behavior

The Atlas SSHPiper plugin lives in `atlas/sshpiper/`.

For each public-key auth attempt:

1. Read the downstream SSH username (`conn.User()`).
2. Call the lookup API with `vm_name=<username>`.
3. Expose the returned `public_keys` as the plugin's `AuthorizedKeys`.
4. If the presented downstream key matches one of those keys, create an
   upstream connection to the returned IPv6 address.
5. Authenticate upstream as `root` using the host private key at
   `/root/.ssh/id_ed25519`.

The plugin only advertises public-key authentication. Password authentication is
not part of the contract.

The upstream host string must be formatted as host-plus-port. For IPv6 this
means `[addr]:22`, not `addr:22`; the plugin uses `net.JoinHostPort` before
handing the address back to SSHPiper.

## Bootstrap contract

`Server.bootstrap()` passes the plugin its credentials through the
`bootstrap-server.py` Task variables:

| Variable / flag | Meaning |
| --- | --- |
| `ATLAS_URL` → `--atlas-url` | Base URL of the Atlas site. |
| `SSHPIPER_LOOKUP_SERVER` → `--sshpiper-lookup-server` | This Server row's UUID. |
| `SSHPIPER_API_KEY` → `--sshpiper-api-key` | The per-server lookup token. |

`bootstrap-server.py` writes these values into `/etc/default/sshpiper` with
mode `0600`; `scripts/systemd/sshpiper.service` loads that file via
`EnvironmentFile=`.

The plugin binary is a bootstrap artifact. Build the Atlas SSHPiper plugin
locally on the Atlas controller for the target server architecture and keep the
built file ready at `scripts/sshpiper/atlas`, the path that
`Server.bootstrap()` uploads. If your local build output is named
`sshpiperd-atlas`, copy or rename it there before bootstrapping. Bootstrap
already copies it to `/tmp/sshpiper-atlas`; the host-side script only installs
that staged file with explicit root ownership and mode:

```
sudo install -m 0755 -o root -g root /tmp/sshpiper-atlas /usr/local/bin/sshpiper-atlas
```

Do not build Go on every host during bootstrap. Bootstrap should install a
prebuilt artifact, not make the host a build machine.

## Host keys

SSHPiper is the SSH server the client sees on port 22. The upstream guest also
has its own SSH host key. This distinction matters for OpenSSH host-key update
features such as `hostkeys-prove-00@openssh.com`; clients may need
`UpdateHostKeys=no` until Atlas deliberately specifies the long-term host-key
presentation model for proxied VM SSH.

## Failure modes

- `too many colons in address`: the plugin returned a bare IPv6 literal instead
  of `[ipv6]:22`.
- `http2: frame too large`: the plugin printed arbitrary data to stdout/stderr
  and corrupted the SSHPiper plugin RPC stream. Plugin code must not print debug
  output directly.
- `Not permitted`: the server token is missing/wrong, or the VM title exists on
  a different server.
- `returned no public keys`: the VM exists but has no usable
  `ssh_public_key` lines.
