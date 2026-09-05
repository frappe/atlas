# Atlas App Specification

[Root specification](../SPEC.md)

[Module specification](atlas/SPEC.md)

## Purpose

The Atlas app provides Frappe settings and provider catalog records for Atlas infrastructure.

## Layout

```text
atlas/                       Atlas module (settings)
  core/                      Provider controllers and the host binary builder
  doctype/                   Settings records
vm/                          Virtual machine records, image records, and image builders
server/                      Server module (catalog)
  doctype/                   Server Size and Server Image records
scripts/                     Host installation scripts
```

## Software

The app uses Python 3.14, Frappe, MariaDB, Redis, Node, and Yarn.

## Scope

The app stores Atlas settings, server catalogs, image metadata, and VM request metadata. Metal owns each VM runtime state and desired state. Atlas does not store a VM lifecycle state machine.

The Virtual Machine name is the Metal VM ID. Creation uses idempotent `PUT /vms/{name}` and accepts HTTP `202`. Atlas uses `GET /vms/{name}` after a lost response. Atlas keeps the draft if the result is uncertain.

Atlas exchanges WireGuard peers, desired cached images, and host capacity with `POST /sync`. Placement uses the latest capacity sample and the image architecture.

Virtual Machine Image is the durable boot artifact for System and Machine images. Each record owns rootfs and kernel objects, exact sizes, and SHA-256 values. Machine image transfer behavior is documented in [VM operations](vm/README.md).

## Host binaries

Atlas builds `metald` and the Atlas WG Mesh CLI after installation and migration. A build occurs only when its source changes.

Atlas publishes each build as a public File and stores the File link in Atlas Settings. Atlas keeps earlier files available.

A host downloads the binary during `install-metald.sh`, so the file needs an address that the host can reach. Set `atlas_base_url` in the site configuration for that address. Atlas uses the site URL when the key is absent.

```json
"atlas_base_url": "https://devfc2.example.com"
```

### Ubuntu build tools

Install these tools before you install or migrate Atlas.

1. Update the Ubuntu package list.

   ```bash
   sudo apt-get update
   ```

2. Install the mandatory build tools and headers.

   ```bash
   sudo apt-get install --yes make clang libbpf-dev linux-libc-dev
   ```

On an offline machine, use an internal APT mirror or install these packages from approved local files.

`make` runs both builds. `clang`, `libbpf-dev`, and `linux-libc-dev` build the eBPF object for Atlas WG Mesh.

The builder uses an installed Go toolchain if it is version `1.26.2` or newer. Otherwise, the builder downloads Go.

The builds also download Go modules. An offline installation needs an installed Go toolchain and a populated Go module cache.

### Manual build

Use these commands to build one binary:

```bash
bench --site SITE build-metald
bench --site SITE build-wg-mesh
```

The command skips the build when the linked File and source hash are current. A missing tool stops the command or migration.

## Validation

Run the Frappe tests described in [the CI workflow](../.github/workflows/atlas-ci.yml).

## Ownership

Keep provider behavior in `atlas/core/server_providers/`. Keep settings behavior in `atlas/doctype/`. Keep catalog behavior in `server/doctype/`. Keep VM orchestration in `vm/virtual_machine_manager.py` and Machine image transfer in `vm/virtual_machine_image_manager.py`.
