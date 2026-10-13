# Set up development

Use this guide to choose the smallest environment for your change. Atlas app work needs a Frappe site. Metal and WG Mesh work needs Linux host features.

::: warning Local setup is a work in progress
The local setup guides are not fully verified. Steps can be incomplete or change. Ask the team if a step does not work.
:::

If this is your first change, read [how a VM request works](../start/how-a-vm-request-works.md). Then use the [code map](code-map.md) to find the owner and its tests. Choose a setup below for that owner.

## Follow a change through the system

1. Read the topic's handbook page to learn the request path, saved state, and failure path. The [architecture](../start/architecture.md) shows which component owns each part.
2. Use the [code map](code-map.md) to find the owner, its nearby `SPEC.md`, and the first test to read.
3. Follow the input from its API or job to the saved record and the worker that applies it. For example, the [firewall guide](../networking/host-networking.md#how-metal-applies-a-firewall-change) follows a network request to the host rule update.
4. Change the owning component and run its focused tests. Use host tests when Linux, ZFS, KVM, or packet behavior matters. Update the authoritative handbook page when behavior changes.

## Choose an environment

| Work | Start here | Needs |
| --- | --- | --- |
| Atlas app, DocTypes, and placement | [Atlas development](atlas-app.md) | Frappe, MariaDB, Redis, and a site |
| Metal code and VM runtime | [Metal development](metal.md) | Go and Linux host tools |
| Metal integration tests | [Metal testing](metal-testing.md) | Linux, root, KVM, ZFS, systemd, and iptables |
| HTTP proxy | [Proxy development](http-proxy.md) | Python 3.14 and OpenResty test tools |
| WG Mesh | [WG Mesh development](wg-mesh.md) | Linux, Go, Clang, libbpf, and a test host for packet checks |
| IPv6 router | [Router design](../networking/ipv6-router.md) and [specification](../../services/ipv6-router/SPEC.md) | Clang and a gateway VM for live checks |

## Run documentation locally

Install the Node dependencies, then start the VitePress server:

```sh
npm install
npm run docs:dev
```

Build the site before you open a pull request:

```sh
npm run docs:build
```

VitePress reads Markdown from the repository. Do not edit `.vitepress/dist` by hand.

## Write documentation

Explain behavior, reasons, and recovery once in the matching handbook section under `docs/`. Keep code contracts in a nearby `SPEC.md` and link to the handbook. A short `README.md` can link to both.

Put a new page in reading order in `.vitepress/sidebar.mts`. Keep source-code links in a `details` block at the end of the page. The `docs-build/internal/` files are background research and are excluded from the site.

## Choose a target branch

`develop` gets all new work. `master` is the stable branch and gets only backports from `develop`.

1. Raise the pull request on `develop`.
2. Add the `backport master` label when the change must go to `master`. After the merge, Mergify opens the backport pull request and assigns it to you.
3. Resolve conflicts in the backport pull request if Mergify reports them, then merge it.

Mergify closes other pull requests to `master`. For a manual backport, add the `manual-backport-pr` label and reopen the pull request.

Mergify merges the base branch into an open pull request that is behind it. It skips drafts and pull requests with conflicts.

Mergify closes a pull request that is open for more than 30 days. To keep it open, add the `keep-open` label.

Mergify labels a pull request by its changed files. An `area:` label shows the component, such as `area: metal`. A `risk:` label shows a change that needs careful review: `risk: migration`, `risk: ebpf`, `risk: api` or `risk: dependencies`. The rules are in `.mergify.yml`.

## Release

A release manager runs the **Release** workflow on `master` from the GitHub Actions tab. The workflow tags the `master` head and creates a GitHub Release with notes from the merged pull requests.

The version starts at `v1.0.0`. A breaking change, marked with `!` such as `feat(metal)!: Remove a field`, increases the minor version. Other changes increase the patch version. The workflow fails when `master` has no commits after the last release.

## Before you submit a change

- Read the nearest component `SPEC.md` before a structural change.
- Run the formatter and focused tests for the component.
- Run `npm run docs:build` when you change documentation or the VitePress configuration.
- Keep each page focused on one reader task.
