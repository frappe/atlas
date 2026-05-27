# Atlas

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

## Verify locally

After installing on an `atlas.local` site, set the DigitalOcean
credentials and run the shared-droplet end-to-end suite:

```bash
bench --site atlas.local set-config -p atlas_do_token <DO_TOKEN>
bench --site atlas.local set-config -p atlas_ssh_key_id <FINGERPRINT>
bench --site atlas.local set-config -p atlas_ssh_private_key "$(cat ~/.ssh/atlas-test)"
bench --site atlas.local execute atlas.tests.e2e.run_all
```

The run takes ~9 minutes and creates one billable droplet that is
deleted at the end. See [spec/README.md](./spec/README.md#testing) for
the full test entry points.

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
