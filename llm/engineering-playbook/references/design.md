# Design defaults

## Principles

- **Simplest design that works.** Reuse an existing block before adding one. One implementation per concept (the `egress` field went away because gateway routes did the same).
- **Stateless controller.** Atlas stores the request; Metal stores what runs. Runtime values read through to the owner. Validation lives mostly in `metald`.
- **One owner per state.** One writer per field. One source of truth (OpenResty map state, not a copy in the control daemon). Keys that must survive a VM loss live in Atlas Settings.
- **Desired state plus reconciler.** Mutations store desired state and return `202`. Reconcilers do full sweeps, so a missed wake costs time, not correctness. Skip unchanged work (hash of applied vs desired). Do not guard against manual host edits.
- **Controllerless where possible.** WG Mesh discovers VM location itself.
- **Generic, not vendor-bound.** "Object Storage", not "S3". Provider registry behind one interface. No hardcoded consumers (Cargo).
- **Ops procedures over guards** for rare operator mistakes.
- **Defer the unneeded:** rate limits, multi-gateway UX, BLAKE3, Price List integration, VM ID recycling.
- **Fleet scale:** 100 hosts, 200 maximum, a few hundred VMs per host. Use it to find bottlenecks and to reject over-engineering.

## Failure and retry

- Fail loudly near the cause. A missing core dependency stops startup.
- Explicit error over silent side effect: return `409` instead of silently changing another setting.
- Retry only safe operations. Background retry loops: try, log, continue. One bad record never blocks a sweep.
- Commit the draft before the remote call. Persist provider resource IDs as soon as they exist.
- Delete cloud resources by stored ID only, never by tag search.
- Idempotent APIs: a repeat returns success with current state. Missing resource returns `404`.
- A retry after a lost response repairs Atlas fields from the owner's actual state.
- Long work never blocks a request; poll status.
- Timeouts are budgets. Placement answers or rejects in about 300 to 500 ms.

## Concurrency and performance

`TimestampMismatchError`, version conflicts, lock wait timeouts, deadlocks and double allocation are bugs. Check before handover:

- **Stale saves:** a job or method that loads a doc, does slow work, then `save()`s races with Desk and jobs. Reload before writing, `db_set` only owned fields, or lock first. Desk actions call a method and reload the form, not save the open form.
- **One writer per field.**
- **Allocation under a lock:** select-then-update on shared resources (IPs, capacity, counters, names) uses `for_update=True` or an advisory lock in one short transaction. Lock one row with `get_value(..., for_update=True)`, not a full document.
- **Lock order and scope:** one fixed order; shortest hold; no network, SSH or provider call under a database lock.
- **Short transactions:** commit per item; no transaction across many records or remote calls.
- **Idempotent retries:** jobs may run twice; the second run sees the first and does nothing harmful.
- **Lock names** include the site or database name.
- **Go:** shared state has one owner goroutine or a mutex; `go test -race` when concurrency changed; defined shutdown and channel ownership.

Name any residual risk in the handover in one line.

Performance:

- Measure first at realistic fleet size.
- `READ COMMITTED` via a decorator on the specific function, not globally.
- Indexes in `on_doctype_update()`.
- Short-TTL negative cache for "pool is full". Remove caches invalidated every second.
- Less chatter: one Metal read per form load, connection reuse, cached settings, skip unchanged syncs.
- Fixed simple constants (1 MiB copy buffer) over adaptive cleverness.

## API design

- **Backward compatible.** Old clients keep working. Add optional fields with safe defaults. Never rename, remove or retype a field, route, status or error code. No `/v2`; evolve additively and keep accepting old request shapes. Same for webhooks, Metal API and generated clients.
- Fixed prefix `/api/atlas`; full words in paths; IDs in paths, objects in bodies.
- Plural resources; singular action verbs: `POST /actions/start`, `/restart`, `PUT /public-ip-allocations/{id}/reserve`.
- `PATCH` for tenant partial updates; Atlas sends a complete object to Metal's `PUT`.
- Remove payloads in the JSON body when asked. Split mixed resources (`/sites`, `/domains`); no generic `{kind}/{key}` routes.
- Tenants pass choices, not internals (no routes). Expose only what callers need: no operation IDs, generations or internal phases.
- Typed request and response models; validate at the boundary.
- One entrypoint when the server decides details.
- Docs: one-line operation titles, consistent verbs (List, Sync, Update, Remove), use-case description, sample response, route-specific status codes only, natural route order. One Scalar `/docs` page.
- OpenAPI is the contract. Regenerate clients on change; CI fails on drift.

## Security

- Whitelist. Unknown doctypes, routes and users get nothing.
- Auth hook: Atlas Admin reaches only `/api/atlas`; System Manager everything; guests only login and docs. No `allow_guest` router flags.
- Privileged flags (`is_privileged`, tenant 0) gated on System Manager and validated on every save.
- mTLS Atlas to Metal and between nodes. ZFS streams through `openssl s_client`/`s_server`.
- No `0.0.0.0` binds. Hosts reachable only over private and WireGuard paths.
- Least-privilege tokens with the smallest audience and lifetime. Secrets in password fields or tight-mode files, never in logs, URLs, commits or docs.
- Password rotation with a 10-minute overlap. Separate cluster and API credentials.
- Never trust `Host` for infrastructure URLs. Verify download digests.
- Security audits only on declared development targets ([environments.md](environments.md)). Try chained exploits. Record accepted risks in [domain.md](domain.md#accepted-risks).

## Desk and data model

- A doctype holds what a human must see. Drop derivable and redundant fields.
- Few explicit statuses; add one only when it changes behavior. Never show a phase that did not happen.
- Archive when unsure.
- Termination protection on core service VMs by default.
- Dev shortcuts behind `developer_mode` or an explicit setting, strict by default.
