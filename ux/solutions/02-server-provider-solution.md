# Server Provider — solution

Maps to [research/02-server-provider.md](../research/02-server-provider.md).

## 1. `Test Connection` and `Provision Server` look identical

### Problem
A read-only API ping (Test) and a billable, multi-minute action
(Provision) render as identical top-bar pills. The operator cannot
distinguish them at a glance.

### Solution

Frappe's `frm.add_custom_button(label, action, group)` already supports
button groups, and `frm.change_custom_button_type(label, group, type)`
sets the button's variant ("default", "primary", "danger", "info").

Use the hierarchy:

- **Provision Server** — primary (dark/coloured pill). Most common
  action on this form.
- **Test Connection** — placed under a **"Actions"** group (the
  three-dot button-group menu). Read-only and cheap; doesn't need
  top-bar real estate.

```js
frm.add_custom_button(__("Provision Server"), provision_handler);
frm.change_custom_button_type(__("Provision Server"), null, "primary");

if (frm.doc.provider_type === "DigitalOcean") {
    frm.add_custom_button(__("Test Connection"), test_handler, __("Actions"));
}
```

### Wireframe

```
Before:                                          After:
┌──────────────────────────────────────────┐    ┌──────────────────────────────────────────┐
│ Test Connection  Provision Server  Save  │    │  Actions ▾   Provision Server   Save     │
│   (grey)           (grey)         (dark) │    │  └ Test Connection            (primary)  │
└──────────────────────────────────────────┘    └──────────────────────────────────────────┘
```

### Frappe components used
- `frm.add_custom_button` with `group` parameter.
- `frm.change_custom_button_type(label, group, "primary")`.

### Fighting Desk?
No.

---

## 2. Provision dialog has no preview, no defaults visible

### Problem
The Provision dialog is one field (`Server Name`) and clicking Provision
silently spends real money. The operator sees no preview of "what's
coming back" — region, size, image, monthly cost.

### Solution

Three changes inside the same standard `frappe.ui.Dialog`:

1. **Read-only preview rows** at the top showing the provider's
   defaults — `region`, `size`, `image`, `monthly_cost`. These are
   pulled live in `before_show` via
   `frappe.db.get_value("Server Provider", name, [...])` plus a
   server method that maps `(region, size)` → estimated monthly cost
   from a small static dict (DO publishes their prices; the dict is
   updated by hand, like `default_image`).
2. **Server-name validation** — block submit until the name matches a
   sane regex (`^[a-z0-9][a-z0-9-]{1,62}$`) and isn't already in use.
   Inline error under the field, not a toast on submit.
3. **Confirmation step.** Use `frappe.warn(title, message_html,
   proceed_action, "Provision")` for the final click — a dialog
   that's explicitly styled red. The body shows:
   _"This will create a `s-2vcpu-4gb-intel` droplet in `blr1` (≈ $24/mo).
   It cannot be paused and starts billing immediately."_

For **`Self-Managed`**, the dialog already has more fields. Same
confirmation step, but the body becomes:
_"Atlas will SSH to `ipv4_address` as `root` and run
`bootstrap-server.sh`. Nothing is created remotely."_ — no cost.

### Wireframe

```
┌────────────────────────── Provision Server ───────────────────────────┐
│                                                                       │
│  Using defaults from bootstrap-provider:                              │
│     Region          blr1                                              │
│     Size            s-2vcpu-4gb-intel    (≈ $24/mo)                   │
│     Image           ubuntu-24-04-x64                                  │
│                                                                       │
│  Server Name *  ┌────────────────────────────────────────┐           │
│                 │ server-blr1-01                         │           │
│                 └────────────────────────────────────────┘           │
│                 lowercase + digits + hyphens, max 63 chars            │
│                                                                       │
│  Provisioning takes ~90 seconds. The new Server form opens            │
│  automatically and the bootstrap Task runs in the background.         │
│                                                                       │
│                                          [ Cancel ]  [ Provision → ]  │
└───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ on click
┌────────────────────────────── Confirm ────────────────────────────────┐
│  ⚠   Create a billable droplet?                                       │
│                                                                       │
│  This will create a s-2vcpu-4gb-intel droplet in blr1 (≈ $24/mo).     │
│  It starts billing immediately and cannot be paused.                  │
│                                                                       │
│                                          [ Cancel ]   [ Provision ]   │
└───────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frappe.ui.Dialog` with `fieldtype: "HTML"` rows for the read-only
  preview block.
- `frappe.warn(title, html, proceed_action, "Provision")` — built-in
  red confirmation dialog (wrapped by `frappe.atlas.confirm_cost`).
- New whitelisted method `Server Provider.preview_cost()` returning
  `{region, size, image, monthly_cost_usd, currency}`.
- Server-name regex enforced client-side; an unknown DO size falls
  back to "—" in the preview.

**Implementation status (landed):** §1 and §2 are wired. The dialog
shows region / size / monthly cost above the Server Name field, blocks
submit on regex mismatch, and on click hands off to `confirm_cost`.
The Self-Managed branch keeps its existing IPv4/IPv6 fields and skips
the cost preview.

### Fighting Desk?
No. `frappe.warn` is the standard pattern.

---

## 3. No "what's coming back" preview after submit

### Problem
After clicking Provision the dialog closes with no toast linking to the
new record; the operator has to navigate to the Server list and figure
out which row is new.

### Solution

The current client script already does `frappe.set_route("Form",
"Server", message)` after the call returns — so the operator *does*
land on the new Server form. The real gap is that the Server itself
sits in `Pending`/`Bootstrapping` for 90 s with no visible progress.

Two-part fix:

1. **Toast linking to the bootstrap Task.** After the dialog returns,
   show `frappe.show_alert({message: "Bootstrap Task: <task-id>",
   indicator: "blue", duration: 6})` with the task id as a clickable
   chip routing to the Task form.
2. **Form intro on the new Server form**, set when `status` is
   `Bootstrapping`: a yellow `frm.set_intro("Bootstrapping… ~90 s.
   Live progress in <a href=...>Task <id></a>.", "yellow")`. When the
   bootstrap Task publishes its `task_update` realtime event, the intro
   refreshes; on success it flips to green; on failure red with a
   `Retry` link to the `Bootstrap` button.

### Wireframe

```
Provision Server clicked
          │
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / Atlas / Server / server-blr1-01                  Pending  ●     │
├──────────────────────────────────────────────────────────────────────┤
│  ⓘ  Bootstrapping… ~90 s. Live progress in Task 7499gpdfl2 →        │
│                                                                       │
│  Provider *           Status *                                       │
│  bootstrap-provider   Bootstrapping                                   │
│  ...                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.set_intro(html, "yellow" | "green" | "red")`.
- `frappe.realtime.on("task_update", handler)` on the Server form
  refreshes the intro.
- `frappe.show_alert` with clickable task-id chip (HTML in `message`).

### Fighting Desk?
No.

---

## 4. `API Token` and `SSH Private Key` unverifiable

### Problem
Password fields are masked but the operator can't tell at a glance "is
this token still valid?" without manually clicking Test Connection.

### Solution

Add two read-only **dashboard indicators** to the form, refreshed when
the form loads (if `provider_type = DigitalOcean`):

- `API Token: ✓ Valid (rate limit 4998/5000)` (green) or `✗ Invalid`
  (red).
- `SSH Private Key: ✓ Format OK` (green) — purely a static client-side
  check that the field parses as an OpenSSH/PEM key.

The API token check is the same one `Test Connection` does, but
auto-runs on form refresh **after 5 min staleness** (cached on the
session). No new server endpoint; we reuse `test_connection`.

For DO key expiry: DO's API exposes a `Date`/`X-RateLimit-Reset` header
but no token-expiry header. The realistic indicator is just "the token
authenticates and has spend permission" — which is what Test Connection
already tells us.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / Server Provider / bootstrap-provider                            │
├──────────────────────────────────────────────────────────────────────┤
│  ● API token valid (rate-limit 4998/5000)   ● SSH key format OK     │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                      │
│  Provider Name        Provider Type                                  │
│  bootstrap-provider   DigitalOcean                                   │
│  ...                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.dashboard.add_indicator(text, color)` — standard top-of-form
  banner.
- Existing `test_connection` whitelisted method.
- Session cache: `frm._atlas_provider_check_at` for the 5-min refresh
  window.

**Implementation status (landed):** The API-token indicator is wired.
On form refresh of a `DigitalOcean` provider the client calls a new
`Server Provider.credential_check` whitelisted method that returns
`{ok, email, rate_limit, rate_remaining}` on success or
`{ok: false, error}` on failure. The form renders a green
`✓ API token valid (4999/5000)` indicator or a red
`✗ API token invalid <reason>` indicator without ever throwing a Frappe
error dialog. The result is cached for 5 minutes per form lifecycle
(`frm._atlas_credential_cache`); the existing **Test Connection**
button under Actions invalidates the cache and re-renders the indicator
on its way out.

**Drift note:**

- The SSH-private-key format indicator from the spec is **not landed.**
  The field is a Password — its value cannot be read client-side, so a
  "format OK" check would have to round-trip to the server. Skipping
  this part: a malformed key already surfaces at the first SSH attempt
  with a clear "Permission denied (publickey)" error in the Task log.
- DigitalOcean's response headers use `RateLimit-Limit` /
  `RateLimit-Remaining` (the IETF draft naming) — not the
  `X-RateLimit-*` shape the wireframe text used. The indicator text
  shows the actual remaining/limit pair regardless of header name.
- Verified in Playwright against a real DO token and a known-bad
  token row (`atlas-e2e-bogus-token`) — green and red indicators both
  render as designed.

### Fighting Desk?
No.

---

## 5. Default Region/Size/Image are free-text Data fields

### Problem
Typos in `default_region = blr2` silently fail at provision time. These
fields are part of DigitalOcean's catalog and could be Select fields
backed by the live API.

### Solution

Two options, in increasing order of cost:

1. **Lightweight (recommended)** — keep the field as `Data`, but use
   `frm.set_query` to power a **live autocomplete** that hits
   `atlas.api.digitalocean.list_regions(provider)`. The user still sees
   a Data field with a typeahead; an unknown value is allowed (we don't
   throw) but the field shows a yellow `description` line "Region
   `blr2` not in account list — provision may fail" while they edit.
2. **Heavy** — convert to `Select` and re-render the field by overriding
   the field's options at refresh time with the live list. This breaks
   the schema's authoritative source-of-truth (the doctype JSON) — fields
   that depend on remote state are best left as Data with an
   autocomplete.

Pick option 1.

The same autocomplete strategy applies to `default_size` and
`default_image`. For `default_size` we also filter the list to "supports
nested virtualisation" — the same constraint the spec already documents
— and show a red description if the operator picks a non-nested-virt
size.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│  Default Region                                                      │
│  ┌─────────────────────────────────────┐                            │
│  │ blr1                              ▾ │   ⓘ                       │
│  └─────────────────────────────────────┘                            │
│    ↓ typeahead, source: list_regions(provider)                      │
│       blr1   Bangalore                                              │
│       nyc3   New York 3                                             │
│       sgp1   Singapore                                              │
│       ...                                                            │
│                                                                      │
│  Default Size                                                        │
│  ┌─────────────────────────────────────┐                            │
│  │ s-2vcpu-4gb-intel                 ▾ │  ⓘ supports nested virt    │
│  └─────────────────────────────────────┘                            │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- Client script: replace the field's plain text widget with
  `frappe.ui.form.ControlData` + `awesomplete` (built-in to Frappe).
- New whitelisted helpers: `atlas.api.digitalocean.list_regions`,
  `list_sizes`, `list_images`. Cached per-provider for 1 hour in
  `frappe.cache()`.

### Fighting Desk?
No — we extend a Data field, we don't replace it.
