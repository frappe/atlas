# Greptile Review Rules

These rules apply to the whole Atlas monorepo. Keep review findings concise and actionable. Do not ask authors to explain a change when the code and diff already make the reason clear.

## Mandatory

- [Mandatory] Flag changes that break an existing client, host or stored data: a renamed, removed or retyped field, route, status value or error code in the Atlas tenant API, Metal API, proxy control API, webhooks or generated clients; a changed default that clients use; a new API version; a schema or data change without a migration patch. Treat these as P1.
- [Mandatory] Flag breaking changes to eBPF programs, maps, keys, values or wire formats unless the change context states the upgrade path.
- [Mandatory] Flag concurrency risks: a document saved after slow work without a reload or lock (`TimestampMismatchError`), two writers for one field, select-then-update without `for_update` or an advisory lock, a network, SSH or provider call while a database lock is held, inconsistent lock order, long transactions across many records or remote calls, job retries that are not idempotent, and advisory lock names without the site or database name.
- [Mandatory] When a test file is modified alongside a bug fix, flag if the test was weakened, loosened, or changed to match the new behavior instead of the behavior being fixed to satisfy the original test's intent.
- [Mandatory] Flag behavior changes that ship without added or updated tests. Do not flag guest scripts, ops scripts, simulators or one-line SSH actions.
- [Mandatory] Flag changes to behavior, interfaces, operations, or structure that do not update the relevant component documentation. Do not ask for docs on internal or expected behavior.
- [Mandatory] Flag documentation files that become too large or cover several topics without being split.
- [Mandatory] Flag cyclomatic complexity above 8.
- [Mandatory] Flag comments that explain what changed or why a change was made, or that describe the previous implementation. Put that explanation in the commit message or change description. Use inline comments only for necessary, non-obvious behavior.
- [Mandatory] Flag explanatory comments placed at the start of a file. Allow required Go package comments, build directives, and license headers.
- [Mandatory] Flag comments and documentation that do not use ASD-STE100 Simplified Technical English.
- [Mandatory] Flag em dashes in comments, docstrings, or documentation.
- [Mandatory] Flag unnecessary line breaks inside comments, docstrings, or documentation paragraphs. Allow structured multi-line Go doc comments, examples, tables, and code blocks.
- [Mandatory] Flag documentation that describes removed behavior or old interfaces, or that repeats content owned by another document.
- Flag verbose comments or comments that restate the code. Use one concise line for a type or method description only when needed.
- Flag committed plan/planning files such as `plan_*.md`.

## Accepted risks

Do not flag the accepted risks and settled items in `llm/engineering-playbook/references/domain.md` (section "Accepted risks") as they stand. Flag a change that touches one, makes it worse, or breaks its stated assumption.

## Commits and pull requests

- Flag commit subjects that do not use the short Conventional Commit format `type(scope): Sentence case`, or that use a scope other than a component: `atlas`, `metal`, `wg-mesh`, `http-proxy`, `ipv6-router`, `wireguard-gateway`, `clients`.
- Flag long commit subjects, and subjects that expose security fix details.
- Flag pull request descriptions that do not use ASD-STE100 Simplified Technical English.
- Flag linked issues that are not on the first line of the description as `Closes #<issue>`.
- Flag bug-fix descriptions that do not state the issue first and then give a short overview.
- Flag overview paragraphs longer than 2 or 3 lines.
- Flag "What changed" bullets that list file names, renames, formatting, comment, test or doc edits, regenerated clients, lint fixes or internal refactor steps that do not change behavior.
- Flag AI co-authors, AI session details, or AI agent metadata in commits and pull requests.

## Testing

- Flag tests that do not verify meaningful behavior, a risk, or a failure case, including tests that only assert hard-coded constants and tests mocked so heavily that they cannot fail.
- Flag tests added only to increase coverage numbers, and separate commits that add coverage without a related behavior or bug fix.
- Flag unclear test names, and test files whose names do not follow a renamed source file.
- Flag tests that need comments but do not explain their intent clearly, and test comments that restate the test code.
- Flag nondeterministic or interdependent tests.
- Flag defensive production code added only to make a test pass when the fixture should change.

## Design and structure

- Flag a second pattern for a job the codebase already does (locks, background jobs, provider calls, SSH scripts, Desk actions, API routes, fixtures), and a better pattern applied in one place while the old one stays elsewhere.
- Flag mutable state with more than one owner, and temporary state that leaks outside its object, module or package.
- Flag scattered behavior: logic in unrelated helpers, provider-specific code outside the provider class, duplicate same-prefix files, or new packages or folders when an existing owner should contain the behavior.
- Flag generic `utils`, `helpers`, `common`, `misc` or `interfaces` modules and packages.
- Flag single-use wrappers and one-line helpers that read better inline, pass-through layers, and dead code.
- Flag clever code when clear code is practical, and new code when existing code can be deleted or simplified instead.
- Flag custom logic that duplicates the standard library, the framework, or an existing repository helper.
- Flag constants for doctype names, field names, labels or route lists, and tables that map one status to another. Constants are for real tunables such as timeouts, retries and limits.
- Flag new configuration options or flags without a real need for a second value.
- Flag broad error handling or fallback logic that hides corrupt or partial state, and retries around operations that are not safe to repeat.
- Flag functions much longer than about 25 lines when they can be split without harming readability, and files over 500 lines when they should be grouped or split.
- Flag unnecessary abbreviations in code, file, API, UI and doctype names.

## Security

- Flag services bound to `0.0.0.0` when a specific address exists.
- Flag missing permission checks on whitelisted methods, tenant data leaks, request data passed to the ORM or a shell without validation, and secrets in URLs, logs, commits or docs.
- Flag trust of the request `Host` header for infrastructure URLs, and downloads used without digest verification.

## Go

- Flag Go changes that add structs, methods, interfaces, or package boundaries without a clear design in the change context. The design should identify ownership, state, method responsibilities, interfaces, and error flow before implementation.
- Flag interfaces introduced speculatively, interfaces with excessive methods, and interfaces defined away from their consumer without a strong reason.
- Flag Go packages without a useful package comment, more than one package comment in a package, and exported declarations without useful doc comments that start with the declaration name.
- Flag unnecessary `Get` prefixes on Go constructors or accessors.
- Flag Go errors that lose useful context or prevent callers from inspecting the original error.
- Flag blocking Go operations that do not accept `context.Context` as the first parameter when context control is needed.
- Flag global mutable state, and unclear ownership of goroutines, shutdown, or channel closing.
- Flag dense Go code packed to save lines, and methods that do too many unrelated things or need verbose comments to be understood.
- Flag cgo, checked-in generated objects such as `.o` files, and `generate.go` files when a `make` target can build them.
- Flag Go changes that do not use `gofmt`, and relevant changes that do not run `go vet` or the race detector, especially concurrency changes.
- Preserve the independent module boundaries: `metal/` and `services/wg-mesh/cli/` each have their own `go.mod`.

## Python and Frappe

- Flag Python behavior placed outside the domain object, manager, or task that owns it, and logic in CLI commands or API routes that should live in that owner.
- Flag Python public functions or important data structures without useful type hints, and `cast()` where a `TYPE_CHECKING` import works.
- Flag relative imports.
- Flag broad or generic Python exceptions when a specific exception is appropriate, and `assert` where `frappe.throw` with a specific exception fits.
- Flag mutable global state, circular imports, and dependency injection that adds complexity without reducing coupling.
- Flag lazy re-exports in package `__init__.py` files.
- Flag `@property` on methods that are not cheap, side-effect-free, no-argument operations returning one noun-like value.
- Flag implementation-only methods without a leading `_`, and methods used outside their owner that are marked private.
- Flag boolean Python properties and methods without an `is_` or `has_` prefix.
- Flag hot-path reads of Single doctypes with `get_single` when `get_cached_value` works, and float fields for sizes or money (use integer MiB and integer cents).
- In `services/http-proxy/`, keep control-daemon behavior in `control/`, OpenResty and Lua behavior in `nginx/`, and tests in `tests/`.
