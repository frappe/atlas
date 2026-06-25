<div align="center">
  <img src="atlas/public/images/atlas-logo.svg" alt="Atlas" width="80" height="80">
  <h1>Atlas</h1>
</div>

Atlas manages Firecracker virtual machines on servers. It is the lowest
layer of a Frappe hosting platform; sites, benches, IAM, and billing live
in separate apps on top.

The spec in [spec/](./spec/README.md) is the source of truth — read it
before changing anything here.

## Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app atlas
```

## Bootstrap

Stand up Atlas on a fresh site — configure settings, provision a server, sync
the base image, and (optionally) bake a golden image, stand up the proxy, and
issue a wildcard cert — in one command. See
[BOOTSTRAP.md](./BOOTSTRAP.md) for the necessary steps and recommended sizing.

## Verify locally

The e2e harness reads its inputs from one JSON fixture, not site config —
`$ATLAS_E2E_CONFIG` (default `~/.cache/atlas-e2e/config.json`). Write the
DigitalOcean credentials and SSH key there once, then run the shared-droplet
suite on an `atlas.tests.local` site:

```bash
mkdir -p ~/.cache/atlas-e2e
cat > ~/.cache/atlas-e2e/config.json <<'JSON'
{
  "do_token": "<DO_TOKEN>",
  "ssh_key_id": "<DO_SSH_KEY_ID>",
  "ssh_private_key_path": "~/.ssh/atlas-test"
}
JSON
bench --site atlas.tests.local execute atlas.tests.e2e.run_all
```

The run takes ~9 minutes and creates one billable droplet that is
deleted at the end. The full fixture shape (TLS, Scaleway, region/size
overrides) is documented in
[`atlas/tests/e2e/_config.py`](./atlas/tests/e2e/_config.py); see
[spec/README.md](./spec/README.md#testing) for the full test entry points.

## Contributing

Install [pre-commit](https://pre-commit.com/#installation) and enable it:

```bash
cd apps/atlas
pre-commit install
```

It runs ruff, eslint, prettier, and pyupgrade.

## License

agpl-3.0

---

- `atlas/` — Frappe app source
- `scripts/` — shell scripts uploaded over SSH and run on the server
- `spec/` — operator-facing specification (source of truth)
- `llm/` — Claude-facing reference material
