# Self-serve bench — remaining work

Status: planning note, 2026-06-05; trimmed to remaining-only 2026-06-08;
**superseded 2026-06-09** — the self-serve site layer is built and in spec.
Companion to [proxy-design.md](./proxy-design.md) and [ideas.md](./ideas.md).

The end-to-end **signup → live Frappe site** flow is built and folded into spec:

- **Spec:** [`spec/14-self-serve.md`](../spec/14-self-serve.md) is the durable
  contract (the three frozen contracts A/B/C, the `Site` / `Site Request`
  DocTypes, the in-guest deploy, the golden bench image, the host-bound e2e).
  Cross-cuts: [`08-images.md` § golden bench image](../spec/08-images.md),
  [`02-doctypes.md`](../spec/02-doctypes.md) (Site #21, Site Request #22),
  [`11-user-ui.md`](../spec/11-user-ui.md) (signup on-ramp + `if_owner` perms),
  and the `v0.9` entry in [`09-roadmap.md`](../spec/09-roadmap.md).
- **Plans:** the build was decomposed into
  [`llm/plans/self-serve/`](./plans/self-serve/00-overview.md) (01 golden image,
  02 Site doctype, 03 deploy-site, 04 signup, 05 e2e, 06 spec/docs), with the
  planned-vs-actual drift tracked in
  [`plans/self-serve/DRIFT.md`](./plans/self-serve/DRIFT.md).

This file's earlier "Tracks to build" + "Contracts to freeze" content is now
fully captured there — the contracts moved into `spec/14-self-serve.md` as their
durable home (the same way `proxy-design.md` was trimmed to rationale +
not-yet-built after the proxy shipped).

---

## Host-proven (2026-06-10)

The end-to-end host run **landed** and the open D01/D03/D05 items are closed —
see [`DRIFT.md` → "Live host run (2026-06-10)"](./plans/self-serve/DRIFT.md):

- **The golden bake** (`bench/build.sh`) ran from scratch on a real droplet
  (snapshot `m6tuh2lmou`, ~7 min — apt/clone/uv/node + the baked `site.local`).
- **`bench setup production` + the `[::]:80` listener** serve the site on v6; the
  deploy's `_enable_ipv6_listeners` is confirmed needed and works.
- **The full signup flow** ran green: `request_site` → `verify()` → cloned golden
  site → `deploy-site.py` → HTTP 200 → Subdomain → proxy reconcile → off-droplet
  HTTPS on **v4 (reserved IP) and v6 (proxy /128)**.
- **The one host-only bug** the run found + fixed: the deploy must repoint
  `default_site` to the FQDN — bench-cli's `frappe serve` resolves the served site
  by `default_site`, not the `Host` header, on a snapshot-booted clone (DRIFT L-1).

Reproduce: bake the golden image once
(`atlas.tests.e2e.use_cases.bench_image.run_smoke`), run
`atlas.bootstrap.run_with_self_serve`, stand up a proxy VM, then `/signup`. The
worker must be up; size the host for ~2 GB per concurrent site (DRIFT L-3).

This file can be deleted now that the work is shipped + host-proven; kept only as
a pointer for in-flight readers.
