# Set up a test region

::: warning Test region setup is a work in progress
This procedure is not fully verified. It uses real provider resources and can cost money. Check current steps with the team before you start.
:::

This guide builds a small test region: one Atlas site, one host, and one VM. Use a test Scaleway project and DNS zone.

Keep the Frappe worker active. Provider setup, catalog sync, and Metal Server provisioning run in background jobs.

Use [`atlas-vm`](../../scripts/atlas-vm/) for an automatic installation in a Firecracker VM. It installs Atlas and completes the settings, provider, DNS, catalog, and system image steps. It does not create a Metal Server.

## Before you start

You need a pilot bench, a test Scaleway project, Route53 access, an SSH key, and an address that hosts can use to reach Atlas. Object storage is optional for the first image. Step 8 explains the site-file option.

The steps below prepare the site, add a host, and create a VM. Step 10 adds the public HTTP proxy.

## 1. Install build tools

Install the tools that build `metald` and WG Mesh on the Atlas host:

```sh
sudo apt-get update
sudo apt-get install --yes make clang libbpf-dev linux-libc-dev
```

See [Atlas development](atlas-app.md#host-binaries) for offline builds and Go toolchain details.

## 2. Create the site

Create a local Atlas site and install the app:

```sh
pilot new-site atlas.localhost
pilot --site atlas.localhost install-app atlas
```

## 3. Set a public Atlas address

For now, a Metal Server must reach the Atlas site to download `metald` and WG Mesh during installation. Use a public Cloudflare Tunnel or ngrok URL for local development.

Set the URL as `atlas_base_url`:

```sh
pilot frappe --site <site> set-config \
  atlas_base_url https://<public-atlas-url>
```

Build and publish the host binaries:

```sh
pilot --site <site> build-metald
pilot --site <site> build-wg-mesh
```

## 4. Configure Atlas Settings

Open **Atlas Settings** in Desk. Enter and save these values:

- Basic settings: set **Server Provider** to `Scaleway` and **Region Name** to `par-1`. Choose an unused **Region ID** from `0` to `65535`, and enter your machine's public SSH key.
- Scaleway: access key, secret key, organization ID, project ID, and zone. Set **Machine Billing Cycle** to `Hourly`.
- Route53: access key ID, access key secret, and wildcard domain. Do not include `*.` in the domain.
- Object Storage: bucket, endpoint URL, region, access key ID, and secret access key.

## 5. Set up the provider and DNS

In **Atlas Settings**, select **Actions** and click these buttons:

1. **Setup server provider** creates or checks the Scaleway resources.
2. **Setup DNS** creates or checks the Route53 zone.

Wait until both actions finish. Atlas marks the settings as complete after both actions succeed.

## 6. Sync the Metal Server catalog

Open **Metal Server Size** and click **Sync**. Then open **Metal Server Image** and click **Sync**.

Wait for the background jobs to finish. The catalog supplies the provider size and Ubuntu image for the Metal Server.

## 7. Create a Metal Server

Open **Metal Server** and create a record. Select `EM-A116X-SSD` and an Ubuntu 24.04 Metal Server Image.

Save the record. Wait for its status to become `Running`. Open its linked **SSH Task** records to see each host command and its result.

## 8. Build a VM image

On Linux, the build needs `curl`, `sha256sum`, `unsquashfs`, `mkfs.ext4`, `truncate`, and `zstd`. On macOS, the build runs in the builder Docker image, which has them.

Build and publish the Ubuntu 24.04 guest image:

```sh
pilot --site <site> build-ubuntu-base-image \
  --version 24.04 --architecture amd64
```

The command uploads the root file system and kernel to object storage. It then creates an Available **Virtual Machine Image**.

Without object storage credentials, add `--storage site-file` to serve both artifacts from the site itself. Atlas moves the image into object storage after you set the credentials in Atlas Settings. See [Images](../storage/image-records.md#artifact-storage).

## 9. Create a VM

Open **Virtual Machine** and click **Create Virtual Machine**. Select the new Virtual Machine Image, then set the CPU, memory, disk, tenant, and network values.

Atlas places the VM on the running Metal Server and sends the desired state to Metal. Keep the record if the first response is uncertain. Atlas reconciles it after Metal confirms the result.

## 10. Create a proxy server

Open **Atlas Settings**, select **Actions**, and click **Renew TLS certificate**. Wait for the wildcard certificate to appear in the Proxy tab.

Open **Public IP Pool** and reserve a provider IPv4 pool, or add a Static IPv4 pool that your network routes to the Metal Servers.

Open **Public IP Allocation** and reserve one tenant-0 IPv4 allocation for each planned proxy.

Open **Proxy Server** and click **Create Proxy Server**. Select the Ubuntu image, set the proxy VM size, and select a Reserved public IPv4 allocation. Repeat this action to create up to five regional proxies. Atlas adds ready nodes to the health-checked `proxy.<wildcard-domain>` address.

Wait for the status to become `Active`. Open its linked **SSH Task** records to see the install output. See [proxy provisioning](../networking/http-proxy/provisioning.md) for engineering details.

## Next steps

- [VM lifecycle](../compute/index.md) explains placement and request recovery.
- [Host provisioning](../region/index.md) explains host setup.
- [Proxy provisioning](../networking/http-proxy/provisioning.md#how-wildcard-tls-is-issued) explains certificate issuance and renewal.
- [Atlas operations](../operate/atlas.md) lists safe checks when setup or provisioning fails.
