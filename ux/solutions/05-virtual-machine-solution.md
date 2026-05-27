# Virtual Machine — solution

Maps to [research/05-virtual-machine.md](../research/05-virtual-machine.md).

## 1. List headlines `description` instead of a stable identifier

### Problem
Multiple rows say "verify vnet_hdr fix". The operator can't tell which
physical VM is which without clicking through.

### Solution

Two complementary fixes:

1. **Show a short, stable ID alongside the description.** The Name is a
   UUID; `name[:8]` is enough to distinguish two VMs with the same
   description. Add a virtual computed column to the list view via
   `frappe.listview_settings["Virtual Machine"].add_columns` plus a
   render function:

   ```js
   frappe.listview_settings["Virtual Machine"] = {
       add_fields: ["status", "ipv6_address", "name", "description"],
       formatters: {
           description(value, df, doc) {
               const short = doc.name.slice(0, 8);
               const label = value || `<i>(no description)</i>`;
               return `${label} <span class="text-muted">· ${short}</span>`;
           },
       },
       get_indicator(doc) {
           return {
               Pending:    [__("Pending"), "orange",  "status,=,Pending"],
               Running:    [__("Running"), "green",   "status,=,Running"],
               Stopped:    [__("Stopped"), "grey",    "status,=,Stopped"],
               Failed:     [__("Failed"),  "red",     "status,=,Failed"],
               Terminated: [__("Terminated"), "grey", "status,=,Terminated"],
           }[doc.status];
       },
   };
   ```

2. **Make IPv6 a copyable chip in the list.** Use a formatter to
   render `[2400:6180:…d001]` as a single-click `Copy` icon. The
   IPv6 is the only stable, useful identifier the operator carries
   around outside the desk.

### Wireframe

```
Before:                                               After:
┌────────────────────────────────────────────┐       ┌─────────────────────────────────────────────────────────┐
│ Description       Server      Status       │       │ Description                Server         Status   IPv6 │
│ verify vnet_hdr   bootstrap…  Pending      │       │ verify vnet_hdr · 8f3cf032 bootstrap…  ● Pending  📋    │
│ verify vnet_hdr   bootstrap…  Pending      │       │ verify vnet_hdr · 489d1578 bootstrap…  ● Pending  📋    │
│ verify carve fix  bootstrap…  Pending      │       │ verify carve fix · e1c48b15 bootstrap… ● Pending  📋    │
│ bootstrap test vm bootstrap…  Terminated   │       │ bootstrap test vm · 543ccac bootstrap… ● Term     📋    │
└────────────────────────────────────────────┘       └─────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frappe.listview_settings.formatters` (standard hook).
- IPv6 copy chip: tiny HTML + `navigator.clipboard.writeText`.

### Fighting Desk?
No.

---

## 2. Pending VM form has no Networking section

### Problem
A Pending VM should already have its IPv6 assigned (the spec says it's
set in `before_insert`), but the form doesn't surface it before
Provision.

### Solution

The data is already populated; the form section is just collapsed by
default (`Networking` is `collapsible`). Two changes:

1. **Show the IPv6 in the header** alongside the status pill, exactly
   like the list view. `frm.dashboard.add_indicator("IPv6 [...]")`.
2. **Auto-expand the Networking section** for `status = Pending`
   (i.e. before provision) so the operator sees the address upfront —
   they'll need to plug it into their DNS or jump host before they
   even click Provision.

```js
if (frm.doc.status === "Pending" && frm.doc.ipv6_address) {
    frm.toggle_display("networking_section", true);
    // Equivalent: open the collapsed section.
    cur_frm.layout.sections
        .find(s => s.df.fieldname === "networking_section")
        .collapse(false);
}
```

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / VM / verify vnet_hdr fix · 8f3cf032            Pending  ●        │
├──────────────────────────────────────────────────────────────────────┤
│ ●  IPv6 [2400:6180:100:d0:0:1:4ae1:d001]  📋  not provisioned yet    │
│ ─────────────────────────────────────────────────────────────────── │
│                                                                      │
│ Description           Status                                         │
│ verify vnet_hdr fix   Pending                                        │
│ ...                                                                  │
│                                                                      │
│ Networking            (auto-expanded for Pending)                    │
│ IPv6 Address          MAC Address                                    │
│ 2400:6180:…:d001      ca:fe:de:ad:be:ef                              │
│ Tap Device                                                           │
│ tap-8f3cf032                                                         │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.dashboard.add_indicator(html, "blue")`.
- `frm.toggle_display`/Section collapse API (standard).

### Fighting Desk?
No.

---

## 3. Terminated VM form is identical to Pending

### Problem
A terminated VM has the same form as a pending one — no "this is gone"
affordance, no "delete the record" action, no "re-provision from this
spec" action.

### Solution

Three pieces:

1. **Strong visual cue** — `frm.dashboard.set_headline_alert(
   "Terminated " + frappe.datetime.comment_when(frm.doc.last_stopped) +
   ". This record is kept for audit; the VM no longer exists.", "red")`.
2. **"Re-provision as new" button** — top-bar, primary (only for
   Terminated). Opens the New Virtual Machine form with the same
   `server`, `image`, `vcpus`, `memory_megabytes`, `disk_gigabytes`,
   `ssh_public_key`, and `description` (+ `" (clone)"`) pre-filled. The
   operator can edit and Save to insert a new row. New UUID, fresh
   IPv6.
3. **"Delete record" button** — top-bar, danger, behind a typed-confirm
   dialog and only allowed when `status = Terminated`. Calls
   `frm.savetrash()` (standard Frappe). The Task rows are preserved
   (FK on `virtual_machine` is `set null` on delete), so the audit
   trail survives.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / VM / bootstrap test vm · 543ccac           Terminated  ●         │
├──────────────────────────────────────────────────────────────────────┤
│  Actions ▾   Re-provision as new          (primary)            Save  │
│  ├ Delete record   (red, typed confirm)                              │
│                                                                      │
│  ⛔  Terminated 1h ago. This record is kept for audit;               │
│      the VM no longer exists.                                        │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                      │
│  Description          Status                                         │
│  bootstrap test vm    Terminated                                     │
│  ...                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.dashboard.set_headline_alert(html, "red")`.
- `frappe.new_doc("Virtual Machine", { ...prefilled... })` for the
  "Re-provision as new" route.
- `frm.savetrash()` (standard delete).

### Fighting Desk?
No.

---

## 4. Lifecycle buttons have no descriptions and no danger styling

### Problem
`Provision`, `Start`, `Stop`, `Restart`, `Terminate` are all default
pills with no per-button hint, and Terminate isn't visually marked as
destructive.

### Solution

Apply the standard three-tier hierarchy from
[03-server-solution.md §1](./03-server-solution.md#1-equal-weight-top-bar-buttons-hide-intent):

| Tier      | Buttons (depending on state) |
| --------- | ---------------------------- |
| Primary   | `Provision` (Pending/Failed) or `Start` (Stopped) or `Stop` (Running) |
| Secondary | `Restart` |
| Hidden    | `Terminate` under `Actions ▾`, **red** |

Each button carries a one-line tooltip via `title` attribute:

- Provision — "Run provision-vm.sh on the host. Takes ~30s."
- Start — "Start the firecracker process. Takes ~5s."
- Stop — "Stop the firecracker process. The disk is preserved."
- Restart — "Stop, then Start. Two Tasks."
- Terminate — "Stop the VM and delete its on-disk artifacts. UUID is
  kept; status becomes Terminated."

**Terminate gets a typed-confirm dialog** with the VM's `description`
(or short ID if blank) plus its IPv6 — the operator must type the
short ID to enable the red button. Same pattern as Reboot in
[03-server-solution.md §5](./03-server-solution.md#5-reboot-has-no-confirmation).

### Wireframe

```
status = Pending:                                  status = Running:
┌────────────────────────────────────────────┐    ┌────────────────────────────────────────────┐
│  Actions ▾   Provision               Save  │    │  Actions ▾   Stop   Restart           Save │
│  ├ Terminate (red)                         │    │  ├ Terminate (red)                         │
└────────────────────────────────────────────┘    └────────────────────────────────────────────┘

Terminate confirm:
┌──────────────────────────── Confirm terminate ────────────────────────┐
│ ⚠   Terminate verify vnet_hdr fix?                                    │
│                                                                       │
│   IPv6   [2400:6180:100:d0:0:1:4ae1:d001]                            │
│   Image  ubuntu-24.04                                                 │
│   Server bootstrap-server-1779879805                                  │
│                                                                       │
│   This deletes the VM's disk artifacts on the host. The UUID and     │
│   Task history are preserved. You can re-provision a new VM from    │
│   this spec via "Re-provision as new" on the terminated row.         │
│                                                                       │
│   Type the short ID to confirm:                                       │
│   ┌─────────────────┐                                                 │
│   │ 8f3cf032        │                                                 │
│   └─────────────────┘                                                 │
│                                                                       │
│                                  [ Cancel ]    [ Terminate ]          │
└───────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.add_custom_button(label, fn, group?)` + `frm.change_custom_button_type(label, group, "primary"|"danger")`.
- Custom dialog with text-match enable on the danger button.

### Fighting Desk?
No.

---

## 5. No "SSH to this VM" affordance

### Problem
The IPv6 is the whole point of the system. The operator has to read it
off the form and paste it into a terminal.

### Solution

The dashboard indicator from §2 doubles as a copy chip:

```
IPv6 [2400:6180:100:d0:0:1:4ae1:d001] 📋
```

Clicking the indicator copies `ssh root@2400:6180:100:d0:0:1:4ae1:d001`
to clipboard (the full command, not just the address). A toast confirms:
`SSH command copied`.

For added discoverability, the **Access** section gets a read-only
"Copy SSH command" button that does the same.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│  Access                                                              │
│  SSH Public Key                                                      │
│  ssh-ed25519 AAAA...                                                 │
│                                                                      │
│  Copy SSH command                                                    │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  ssh root@2400:6180:100:d0:0:1:4ae1:d001                  📋  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `navigator.clipboard.writeText`.
- `frappe.show_alert({message: "SSH command copied", indicator: "green"})`.
- HTML field in the Access section.

### Fighting Desk?
No.

---

## 6. Creation form is too generic

### Problem
- `Description` is optional but the *only* identifier the operator sees.
- `SSH Public Key` is a paste every time.
- Resources default to `1 / 512MB / 4GB` with no preset.
- No cost preview, no "this VM will use X of Y available cores".

### Solution

**Inline guidance + presets** — no schema change. Three pieces:

1. **Description warning** — when empty on save, `frm.set_intro(
   "Without a description the list will show only a UUID. Add at least
   a one-word label.", "yellow")`. Not a blocker; just a nudge.
2. **SSH key auto-fill** — pull the provider's `ssh_private_key` (it's
   a Password; we cannot show it client-side) **but** add a new
   `default_vm_public_key: Code` field on Server Provider (operator
   provides their *public* key). On VM creation, if `ssh_public_key`
   is empty, pre-fill it from the provider's `default_vm_public_key`.
   A small "Reset to provider default" link sits below the textarea.
3. **Size presets** — three radio buttons under the Resources header:
   `Small (1 vCPU / 512 MB / 4 GB)`, `Medium (2 / 2 GB / 10 GB)`,
   `Large (4 / 8 GB / 40 GB)`, `Custom (current behavior)`. Defaults
   to Small. Each preset writes the three Int fields. Implemented in
   the client script via radio buttons in an HTML region — no new
   schema fields.
4. **Capacity preview** — small dashboard indicator showing
   `Server capacity: X vCPUs used / Y total (Z VMs)`. The total comes
   from the Server's `size` parsed against a static dict (DO publishes
   per-size vCPU counts; small static map). "Used" sums `vcpus` of
   non-Terminated VMs on this server. If `vcpus + used > total`,
   render the indicator red and add a yellow intro: "Server is
   oversubscribed — Provision may fail."

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / VM / New                                                         │
├──────────────────────────────────────────────────────────────────────┤
│  ● Server capacity: 2 vCPUs / 2 total (4 VMs)   ⚠ oversubscribed     │
│  ⓘ Without a description the list will show only a UUID.             │
│                                                                      │
│  Description                Server *                                 │
│  ┌─────────────────────┐    ┌─────────────────────────┐             │
│  │                     │    │ bootstrap-server-…    ▾ │             │
│  └─────────────────────┘    └─────────────────────────┘             │
│                                                                      │
│  Image *                                                             │
│  ┌─────────────────────┐                                            │
│  │ ubuntu-24.04      ▾ │                                            │
│  └─────────────────────┘                                            │
│                                                                      │
│  Resources                                                           │
│  Preset:                                                             │
│  ⦿ Small  (1 / 512 MB / 4 GB)                                       │
│  ◯ Medium (2 / 2 GB / 10 GB)                                        │
│  ◯ Large  (4 / 8 GB / 40 GB)                                        │
│  ◯ Custom                                                            │
│                                                                      │
│  vCPUs       Memory (MB)       Disk (GB)                             │
│  [ 1 ]       [ 512 ]           [ 4 ]                                 │
│                                                                      │
│  Access                                                              │
│  SSH Public Key                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ ssh-ed25519 AAAA... aditya                                   │    │
│  └──────────────────────────────────────────────────────────────┘    │
│  ⓘ Pre-filled from provider's default VM key. Reset to default.      │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.set_intro(html, color)` for the description nudge.
- New optional `Code` field `default_vm_public_key` on Server Provider
  (schema change — small, justified by the spec gap).
- HTML field for the preset radios.
- Dashboard indicator (`frm.dashboard.add_indicator`).

### Fighting Desk?
No. One new field on Server Provider is the only schema addition; the
rest is client script + dashboard.
