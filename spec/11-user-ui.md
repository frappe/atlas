# User UI — removed (Central owns end-users)

> The operator UI is [10-desk-ui.md](./10-desk-ui.md). This chapter used to
> describe Atlas's *second audience* — end users — and the owner-scoped permission
> boundary that contained them. **That whole layer has been removed.** It is kept
> here as a short pointer so links resolve and the history is legible.

## What was removed and why

Atlas once served two audiences: operators on Desk, and end **users** who held an
`Atlas User` role and owned their own VMs / Snapshots / SSH Keys / Sites under a
row-level (`if_owner` + `permission_query_conditions`) boundary, reached first
through a Vue 3 `/dashboard` SPA and later through a guest `/signup` → `/verify`
on-ramp.

Under the Central pivot ([16-central.md](./16-central.md)), **Central is the sole
customer-facing front door**: it owns identity, teams, billing, and signup. A
second in-Atlas end-user surface is redundant, so it was all removed:

- the `/dashboard` SPA (Vue 3 + frappe-ui) and its host page / route rule;
- the `/signup`, `/verify`, and `/site-status` guest web surface, the
  `atlas.atlas.api.signup` endpoint, the `Site Request` doctype, the verification
  email + outbound-email setup, and the `User` creation it drove;
- the `Atlas User` role fixture and the row-level permission layer
  (`atlas/atlas/permissions.py` — `owner_only` / `task_by_owned_vm` /
  `task_has_permission` — and the `if_owner` permission rows on Virtual Machine,
  Virtual Machine Snapshot, SSH Key, Site, Task);
- the coupled tests (`test_permissions.py`, `test_api_signup.py`,
  `test_site_status.py`, and the owner-scoping cases in `test_site.py` /
  `test_ssh_key.py` / `test_placement.py`).

## What replaces it

Atlas is now **operator/Central-facing only** (System Manager). Tenancy
attribution rides a `tenant` link (→ [Tenant](./16-central.md)) on the resources
Central provisions, not an end-user `owner`:

- **Sites** — Central calls `atlas.atlas.api.site.create_site(central_reference,
  subdomain, …)`; Atlas get-or-creates the Tenant, inserts the Site, and reports
  progress back via `site.*` events + the `get_site` poll. See
  [14-self-serve.md](./14-self-serve.md).
- **VMs** — Central calls `atlas.atlas.api.provision.create_vm(...)`; same Tenant
  stamping + event reporting. See [16-central.md](./16-central.md).

There is no guest-reachable write surface in Atlas anymore. The placement model
(the operator controls which servers are Active and which images exist; the
controller fills `server`/`image` in `before_insert` from a vCPU-budget) is
unchanged — see [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md)
and [placement.py](../atlas/atlas/placement.py).
