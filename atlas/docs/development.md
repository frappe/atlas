# Atlas development

Run Atlas commands from the pilot (new bench). Always name the site.

## Static checks

Run Ruff after each Atlas review slice:

```sh
ruff check atlas
```

## Frappe tests

Use a dedicated test site. Do not use a development site for tests.

```sh
pilot --site TEST_SITE set-config allow_tests true
pilot --site TEST_SITE run-tests --app atlas
```

The CI workflow creates `test_site`, installs Atlas, builds assets, and runs the complete app test suite.

## Host integration needs

Atlas unit and Frappe tests do not prove provider or host integration. A full Server check needs these external resources:

- Valid provider credentials and quota.
- A provider private network.
- A reachable public IPv4 address.
- Root Secure Shell access.
- The host build tools.
- KVM, ZFS, systemd, iptables, and Atlas WG Mesh.

Check provider creation, a safe setup retry, each power action, disk inventory, and provider deletion on a test host.
