# Atlas development

Run Atlas commands from the pilot bench. Always name the site.

## Atlas in a local VM

`scripts/atlas-vm/run.sh up` runs Atlas itself in a local Firecracker VM. It builds the guest image, boots the VM, installs Pilot, creates the bench and the site, and leaves the site up. See [scripts/atlas-vm/README.md](../../scripts/atlas-vm/README.md).

## Static checks

Run Ruff before you submit an Atlas change:

```sh
ruff check atlas
```

## Frappe tests

Use a dedicated test site. Do not use a development site for tests.

```sh
pilot --site TEST_SITE set-config allow_tests true
pilot --site TEST_SITE run-tests --app atlas
```

The CI workflow installs Atlas, builds assets, and runs the complete app test suite on a test site.

## Host integration needs

Atlas unit and Frappe tests do not prove provider or host integration. A full Metal Server check needs these external resources:

- Valid provider credentials and quota.
- A provider private network.
- A reachable public IPv4 address.
- Root Secure Shell access.
- The host build tools.
- KVM, ZFS, systemd, iptables, and Atlas WG Mesh.

Check provider creation, a safe setup retry, each power action, disk inventory, and provider deletion on a test host.

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
