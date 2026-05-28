# Phase 3 — Server Provider (post-implementation wireframe)

Touches: schema (set_only_once + on-disk SSH key), controller
(validate immutability + archive), Provision dialog (standard Selects),
form actions (Archive).

## Form layout (saved Server Provider) — read-only after insert

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Server Provider / bootstrap-provider          Save (subtle)             │
│                                                Provision Server (primary)│
│                                                Actions ▾                 │
│                                                  Test Connection         │
│                                                  Archive (danger)        │
├──────────────────────────────────────────────────────────────────────────┤
│  ✓ API token valid (4998/5000)                          (green chip)     │
├──────────────────────────────────────────────────────────────────────────┤
│  Provider Name      [bootstrap-provider]  (read-only — set_only_once)    │
│  Provider Type      [DigitalOcean       ]  (read-only)                   │
│  Is Active          ☑                       (read-only)                  │
│                                                                          │
│  Authentication                                                          │
│  ─────────────────────                                                   │
│  API Token              [●●●●●●●●●●●●]    (read-only, masked)            │
│  SSH Key ID             [fp:fingerprint]  (read-only)                    │
│  SSH Private Key Path   [/etc/atlas/keys/bootstrap-provider.pem]         │
│                                            (read-only, Data not Password)│
│                                                                          │
│  Defaults for new servers                                                │
│  ─────────────────────                                                   │
│  Default Region    [blr1]                (read-only)                     │
│  Default Size      [s-2vcpu-4gb-intel]   (read-only)                     │
│  Default Image     [ubuntu-24-04-x64]    (read-only)                     │
└──────────────────────────────────────────────────────────────────────────┘
```

## Provision Server dialog (DigitalOcean)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Provision Server                                              [×]       │
├──────────────────────────────────────────────────────────────────────────┤
│  Server Name *                                                           │
│  [_________________________________]                                     │
│  lowercase + digits + hyphens, max 63 chars                              │
│                                                                          │
│  Region *                                                                │
│  [blr1                ▾]   (Select, defaults to provider.default_region) │
│                                                                          │
│  Size *                                                                  │
│  [s-2vcpu-4gb-intel   ▾]                                                 │
│                                                                          │
│  Image *                                                                 │
│  [ubuntu-24-04-x64    ▾]                                                 │
│                                                                          │
│                                                  [Cancel]  [Provision]   │
└──────────────────────────────────────────────────────────────────────────┘
```

No preview HTML block. No cost-table. No "Provisioning takes ~90s"
footer. The cost-confirm `frappe.warn` still fires after Provision is
clicked.

## Archive Actions item

```
Actions ▾
  Test Connection
  Archive            (red tonal hover, danger button in confirm dialog)
```

Archive opens a `confirm_destructive` dialog asking the operator to
type the provider name to confirm. Clicking Archive runs
`provider.archive()` which sets `is_active=0` via `db.set_value`.

## What changed

1. **Schema** ([server_provider.json](../../../atlas/atlas/doctype/server_provider/server_provider.json))
   - Dropped `ssh_private_key` (Password) field
   - Added `ssh_private_key_path` (Data, reqd, set_only_once)
   - Added `set_only_once` on `provider_name`, `provider_type`,
     `is_active`, `api_token`, `ssh_key_id`, `default_region`,
     `default_size`, `default_image`
2. **Controller** ([server_provider.py](../../../atlas/atlas/doctype/server_provider/server_provider.py))
   - `validate` now calls `_validate_provider_type_requirements` and
     `_validate_immutability`
   - `_validate_immutability` walks `IMMUTABLE_AFTER_INSERT` and
     throws when any post-insert change is detected
   - New `archive()` whitelisted method
   - `provision_server` accepts `region`/`size`/`image` kwargs;
     they default to provider defaults
3. **SSH key from disk**
   - New `atlas.atlas.secrets.get_ssh_key_from_disk(path)` helper
   - `connection_for_server` reads `ssh_private_key_path` and slurps
     contents via the helper; `Connection` shape unchanged
4. **Migration patch** ([migrate_ssh_key_to_disk.py](../../../atlas/patches/v1_0/migrate_ssh_key_to_disk.py))
   - Pre-model-sync. For each Server Provider, reads decrypted
     `ssh_private_key`, writes to `/etc/atlas/keys/<name>.pem`
     (0600), sets `ssh_private_key_path`
   - Falls back to `~/.atlas/keys/<name>.pem` when `/etc/atlas/keys`
     isn't writable (local dev sites)
5. **API** ([provider_options.py](../../../atlas/atlas/api/provider_options.py))
   - Hand-maintained `KNOWN_REGIONS` / `KNOWN_SIZES` / `KNOWN_IMAGES`
     lists exposed via `provider_options()` whitelisted method
6. **JS** ([server_provider.js](../../../atlas/atlas/doctype/server_provider/server_provider.js))
   - Provision dialog: editable Selects (Region/Size/Image) instead
     of preview HTML
   - Archive Actions item with destructive-confirm dialog
7. **Tests**
   - `test_server_provider.py` — added `TestServerProviderImmutability`
     (3 tests) and `TestGetSshKeyFromDisk` (2 tests)
   - `test_permissions.py` — `ssh_private_key` → `api_token` for the
     plaintext-not-in-doc test (analogous secret, still on the row)
   - `fixtures.py::make_provider` — uses `ssh_private_key_path`
     pointing at a tempfile
   - `bootstrap.py` — reads `atlas_ssh_private_key_path` site config
     instead of `atlas_ssh_private_key`
   - e2e `_config.py::get_ssh_private_key_path()` — replaces
     `get_ssh_private_key()`; falls back to legacy `atlas_ssh_private_key`
     by spilling to a tempfile so existing e2e configs still work
