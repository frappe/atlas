# Setup

Use this guide to make a new proxy VM image or to start a VM from a base snapshot.

## Before you start

Use Ubuntu 24.04. Run all setup commands as `root` or with `sudo`. Give the VM network access to the Ubuntu and OpenResty package servers.

The setup script creates an empty authentication file and a placeholder certificate. The script does not replace these files if they already exist. The proxy can start before the controller sends its configuration.

## Make a new base VM image

1. Copy this repository to a new Ubuntu VM.
2. Run the setup script.

  ```sh
  sudo ./nginx/setup.sh
  ```

3. Do a check that the services are enabled.

  ```sh
  sudo systemctl is-enabled openresty.service atlas-proxy-control.service
  ```

4. Shut down the VM.
5. Make a snapshot of the VM.

The setup script enables both services. They start when the VM starts after the snapshot.

## Start a VM from a base snapshot

1. Start the VM.

2. Let cloud-init write the htpasswd file at `/etc/atlas/proxy-control.htpasswd`.

3. Let cloud-init restart `atlas-proxy-control.service` after it writes the file.

4. Do a check of the services.

```sh
sudo systemctl status openresty.service atlas-proxy-control.service
curl -kfsS -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" https://127.0.0.1:9000/healthz
curl -kfsS -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -o /dev/null https://127.0.0.1:9000/readyz
```

5. Send the wildcard certificate and the full maps from the controller.

Do not direct public DNS traffic to the VM before the controller sends the required configuration.

## Cloud-init authentication file

The file must contain one bcrypt htpasswd entry. Use a fixed user name such as `atlas`.

```text
atlas:$2y$05$examplebcryptpasswordhash
```

The daemon rejects every request if the file is missing, empty, or not valid.

Use cloud-init to write the file and restart the daemon.

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

1. Send a `PUT /v1/certificate` request with the wildcard domain, certificate, and private key.

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
