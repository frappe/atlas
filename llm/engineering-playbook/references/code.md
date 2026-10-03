# Code and refactoring

## Taste

- Readable by a new joiner during an outage. Simple, not clever ("simple code is much better than clever code").
- Least code: fewer lines, less complexity. Net negative is good.
- Not dense: blank lines between logical blocks; multi-line over packed one-liners ("dont make go codes dense to save loc").
- One way to do one thing: find how the codebase already does the job (lock, job, provider call, SSH script, Desk action, route, fixture) and follow it. A clearly better way replaces every use in the same change when small; otherwise propose it separately. Never leave two.
- Do what was asked, read literally.

## Naming

Naming draws the most corrections. Review every new name.

- Full words in code and file names. A Go receiver may be short. Examples: `opts` → `options`, `mem_mib` → `memory_mib`, `hostcmd` → `hostexec`, `C4_LEN` → `WIREGUARD_CONTROL_PACKET_LENGTH`.
- Names say what the thing does. Rejected: `delete_absent`, `is_at_or_below`, `claim`, `armWake`, `PortableConfig`, `machine_handle.go`, `select_host` that returns nothing. Better: `pump_output` → `stream_output`, `sweep` → `reconcileAll`, `enqueue_migration` → `reconcile_migration`.
- Booleans: `is_` or `has_`. No-argument computed noun: `@property`. Work with arguments: `get_<thing>()` or a domain verb.
- No project prefix inside the project ("call it just get_config"). No folder name repeated in file names.
- Fields named for meaning and unit: `disk_mib`, `throughput_mibps`, `cpu_millicores`, `hourly_pricing_usd_cents`. No `slug`. `last_known_state` (stored) vs `current_state` (polled). Purpose, not environment.
- API, UI, doctype names in full words: `/virtual-machines`, "Start VM", "Edit Network Throughput".
- Symmetric peers (`VirtualMachineReconciler` / `ImageReconciler`). Remove ambiguity (`detach_ipv4_address`, `attach_public_ipv6`).
- Revert a worse rename. A rename updates all callers, tests, docs and generated clients.

## Structure

- Object-oriented: behavior on its owner (doctype methods, provider classes, a `Tracker` struct). Static constructors on doctypes (`Server.provision(...)`). Static methods are fine; loose module functions are not.
- Thin controllers and API routes: transport and permission only; core behavior works from Desk. `validate` calls focused methods. A `service.py` in the doctype folder is fine when the controller grows.
- Class order: properties, abstract, public, static and class methods, private last. Globals and constants at file top.
- `@override` on implemented abstract methods. `@register` registry for pluggable strategies and providers.
- Cyclomatic complexity 8 or less. Functions around 25 lines when a split keeps readability; never split into single-use helpers.
- Fewer, well-named files of about 100 to 500 lines. Merge tiny single-use files and packages (`idalloc`); leaf utilities in one place (`internal/platform`). Check import cycles. Only entrypoints under `cmd/`.
- Group related files in folders (`service/core/proxy/`, `scripts/aws/`). No `utils`, `helpers` or `common`. One-line helpers stay inline.
- Public imports through package `__init__`. Absolute imports only.
- Exceptions in a shared boundary module (`atlas/atlas/core/exceptions.py`).
- Type hints on public functions. `TYPE_CHECKING` imports, never `cast()`. Pydantic at the API boundary; never pass a request dict into the ORM.

## Comments and docstrings

- One-line docstring per method in plain words.
- Inline comments only for what code cannot say: business rule, invariant, external quirk, concurrency rule.
- A longer block is welcome for a genuinely complex algorithm (cluster election, multicast retry rationale with RFC precedent). Do not trim away real rationale.
- Never: file-top comments, `#:`, "previously", change explanations, hints of the old pattern.
- Short section comments in long entrypoints (`metald serve`, setup scripts) when asked.

## Constants and configuration

- Constants only for real tunables: timeouts, retries, limits, sizes. Not for field names, doctype names, labels, method sets, route lists or status maps.
- No table that renames one state to another; use the same values.
- One limit, one constant, used by every dependent check.
- Round or power-of-two limits (1024, 32 tags, 256 KiB).
- Hardcode until a second value is real. Remove unused options (`pilot.branch`, `is_proxy_docs_auth_required`).
- Config in TOML, one section per thing, no commented-out alternatives.

## Python and Frappe

- Framework first: `cbool`, `get_cached_value`, `get_value(..., for_update=True)`, `advisory_lock`, `filelock`, `make_autoname`, `on_doctype_update()` for indexes, `frappe.throw(..., exc=SpecificError)`. Check Frappe source before assuming (six-field cron is valid).
- `frappe.throw`, not `assert`. One error path.
- Set fields and `save()`; `flags.ignore_validate` when needed. No `get_value` plus `reload`, `_save_x` wrappers or low-level password writes. Optional persistence: `save: bool = True` argument.
- Few manual commits. Loops over records: per item try, work, commit; on error rollback, `log_error`, continue. Job boundaries commit already. `# nosemgrep` only where a commit is required.
- Jobs: stable `job_id`, queue and timeout set; a scheduled job retries pending work; run as Administrator through a decorator that restores the user in `finally`.
- Autoname `By Script` with series that never release numbers (`vm-<tenant>-.####`). Unique constraints via doctype JSON `unique`.
- Units: integer MiB (ceil), integer cents, UTC, Unix timestamps in API responses. Frappe has no long int; floats lose data.
- Hot-path settings via `get_cached_value`. Read Metal once per form load (`cached_property`, request cache).
- CLI commands follow Frappe (`@click.command`, `pass_context`, `frappe.init/connect/destroy`), are named by action (`build-metald`), and print subprocess input and output.
- Python over long shell; no Python in shell heredocs. `string.Template` for rendered config. `pyproject.toml`, latest stable libraries, `httpx` for Unix sockets.

## Doctype and dialog layout

Operators read forms during incidents.

- **Approval first:** mockup (sections, columns, field types) before editing doctype JSON for any new, moved or removed field, section, column or tab.
- **Balanced:** two columns where fields allow, similar heights, related fields side by side (size, IOPS, throughput; IPv4 and IPv6). No lone field or empty column.
- **Grouped:** short section titles ("Virtual Machine Specs", "Network"). Runtime and provider data in its own section, collapsed when long.
- **Short descriptions:** most fields need none. One short line, shown on click. No warnings under fields; use a confirmation or a note on the final step.
- **Labels:** full words, sentence case, unit included ("Disk (MiB)"), match the type.
- **Stable:** no layout jump when a value changes.
- **Consistent:** copy section names, action groups and field order from similar doctypes in the module.

## Desk UI

- **Actions** and **Dangerous Actions** (with confirmation) groups, same JS pattern everywhere. Explicit labels ("Snapshot VM", "Update disk in-place").
- Hide actions that do not apply to the provider or state.
- Action-managed doctypes (VMs, proxies, migrations): disable direct create and save; custom create dialog.
- Dialogs: default size, sensible defaults, dropdowns for choices, `freeze: true`, `msgprint` when queued. Section titles, not CSS hacks.
- Number cards without color. Sidebar grouped as specified.
- Secrets behind `frappe.only_for("System Manager")` plus a System User check.
- Vendor front-end libraries locally; no CDN.

## Go

- Concrete types; small consumer-side interfaces only for a real need.
- Package comment per package; doc comment per exported declaration starting with its name; one line usually.
- `NewType` constructors, no `Get` prefix.
- Follow the library's official style; for Cobra: `var xCommand = &cobra.Command{...}` per file, one registration place, `PersistentPreRunE` for shared checks, help ordered by workflow ("dont add our own shits even some part gets duplicated").
- Small CLIs: one or two files, few dependencies, embedded BPF object.
- No cgo, no checked-in `.o` or `generate.go`; build via minimal, consistent `make` targets (`bpf`, `build`, `clean`, `test`, `vet`).
- Remove nil-receiver hacks, shadowed names, argument-order traps, repeated blocks. Make the central rule visible (remove unwanted state, then build wanted state).
- One helper per job with a clear signature (`RunInNetworkNamespace(...) (string, error)`; callers ignore output with `_`).
- Before handover use `llm/go-code-review-guide.md`.

## eBPF and C

- Keep it simple: few files named by content (`state.h`, `control.h`, `protocol.h`, `*_hook.h`), named arguments, small named map-lookup helpers.
- Comment only non-obvious packet or protocol behavior.
- Hardcode fixed prefixes (`fdaa::/16`, `fdab::/16`); maps or CLI for growing sets.
- No features before they are needed (rate limits, debug events).
- Diff against the previous version before a rewrite so no security check drops.
- Avoid breaking maps, keys, values and wire formats; add new maps or fields old programs ignore. In-place upgrades keep VMs reachable. Unavoidable break: propose the upgrade path first.

## Shell and ops scripts

- `set -euo pipefail`, `bash -n` clean, `apt` not `apt-get`, mode 755.
- Output: `==> step` and `error:` lines.
- No debug leftovers. Hardcode instead of options. Required inputs as positional arguments.
- Idempotent reruns; reuse or recreate cleanly; single instance via `flock`.
- Destructive defaults need an opt-out flag and a strong warning (`--keep-downloads`).
- Generate long random passwords; print each new one once to the operator's terminal. Agents never repeat secrets.
- Short readiness timeouts, bounded retries, then fail with body and log tail.

## Refactoring patterns

1. **Inline single-use helpers** ("dont keep adding lot of private functions").
2. **Delete pointless layers:** wrappers, pass-through re-exports, unused modules, simulator self-tests, empty folders.
3. **Merge cohesive files** (`errors.py` into `client.py`) while under 500 lines.
4. **Move behavior to its owner:** provider steps into the provider class; image lookup on `VirtualMachineImage`; permissions in one `overrides.py` whitelist that denies unknown doctypes.
5. **Template method:** base class runs the step list; providers declare steps; common steps in the base.
6. **Explicit flags over fragile status** (`is_provisioning_completed`).
7. **No stored derived data:** derive it or make it a virtual field reading through to Metal.
8. **Child table** for repeated records humans read; `Small Text` or JSON `Code` for simple lists.
9. **Smaller new surface:** fewer endpoints, fields and flags; one safely rerunnable action over partial ones. Never remove or rename an existing public field or route.
10. **Naming pass** on every touched file, including test file names.
11. **Verbosity pass** on every touched file, unchanged lines too.
12. **Contributor slop review:** commit by commit, list anti-patterns and inflation, propose the smaller shape, refactor after approval.

Package audit: good method names, not dense, minimal explanation, short concept doc or SPEC, one-line docstrings, natural method order, SPEC not duplicating code.
