# Setup

Use this guide to make a proxy VM image or start a VM from a snapshot.

## Before you start

Use Ubuntu 24.04 and `root` or `sudo`. Give the VM access to the Ubuntu, OpenResty, and deadsnakes package servers.

The daemon needs Python 3.14. The setup script adds the deadsnakes PPA and creates its virtual environment with `python3.14`.

The setup script creates the locked `frappe` account with passwordless `sudo`. The image builder removes `ubuntu`, sets `frappe` as the cloud-init user, and sets the hostname to `atlas-proxy`. Add an SSH key with cloud-init. The script also creates an authentication file and placeholder certificate if they do not exist.

## Make a new base VM image

1. Copy this repository to an Ubuntu VM.
2. Run the setup script:

  ```sh
  sudo ./nginx/setup.sh
  ```

3. Check that both services are enabled:

  ```sh
  sudo systemctl is-enabled openresty.service atlas-proxy-control.service
  ```

4. Shut down the VM and make a snapshot.

The setup script enables both services. They start when the VM starts after the snapshot.

## Start a VM from a base snapshot

1. Start the VM.
2. Use cloud-init to write `/etc/atlas/proxy-control.htpasswd` and restart `atlas-proxy-control.service`.
3. Check the services:

```sh
sudo systemctl status openresty.service atlas-proxy-control.service
curl -fsS -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_PASSWORD" http://127.0.0.1:9000/healthz
curl -fsS -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_PASSWORD" -o /dev/null http://127.0.0.1:9000/readyz
```

4. Send the wildcard certificate and full maps from the controller.

Do not direct public DNS traffic to the VM before the controller sends the required configuration.

## Cloud-init authentication file

The file must contain one bcrypt htpasswd entry, with a fixed user name such as `atlas`.

```text
atlas:$2y$05$examplebcryptpasswordhash
```

The daemon rejects every request if the file is missing, empty, or invalid. Use cloud-init to write the file and restart the daemon:

```yaml
write_files:
  - path: /etc/atlas/proxy-control.htpasswd
    owner: root:root
    permissions: '0640'
    content: |
      atlas:$2y$05$replace-with-a-bcrypt-hash
runcmd:
  - systemctl restart atlas-proxy-control.service
```

## Configure the VM after start

1. Send `PUT /v1/certificate` with the wildcard domain, certificate, and private key.
2. Send `PUT /v1/sites` with the full site map.
3. Send `PUT /v1/domains` with the full custom-domain map.

See [Control daemon](control-daemon.md) for request examples.

## Use an existing configured snapshot

An existing configured snapshot can contain an old certificate and old maps. The proxy loads that data when it starts.

Send the current certificate and full maps before you direct traffic to the VM. Use a base snapshot with empty maps and the placeholder certificate when possible.

## Service commands

```sh
sudo systemctl status openresty.service atlas-proxy-control.service
sudo systemctl restart openresty.service
sudo systemctl restart atlas-proxy-control.service
journalctl -u openresty.service -f
journalctl -u atlas-proxy-control.service -f
```
