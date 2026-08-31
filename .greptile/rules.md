# Greptile Review Rules

These rules apply to the whole Atlas monorepo. Keep review findings concise and actionable. Do not ask authors to explain a change when the code and diff already make the reason clear.

## Mandatory

- [Mandatory] When a test file is modified alongside a bug fix, flag if the test was weakened, loosened, or changed to match the new behavior instead of the behavior being fixed to satisfy the original test's intent.
- [Mandatory] Flag behavior changes that ship without added or updated tests.
- [Mandatory] Flag changes to behavior, interfaces, operations, or structure that do not update the relevant component documentation. Keep documentation current in the same change.
- [Mandatory] Flag documentation files that become too large or cover several topics without being split.
- [Mandatory] Flag cyclomatic complexity above 8.
- [Mandatory] Flag comments that explain what changed or why a change was made. Put that explanation in the commit message or change description. Use inline comments only for necessary, non-obvious behavior.
- [Mandatory] Flag explanatory comments placed at the start of a file. Allow required Go package comments, build directives, and license headers.
- [Mandatory] Flag comments and documentation that do not use ASD-STE100 Simplified Technical English.
- [Mandatory] Flag em dashes in comments, docstrings, or documentation.
- [Mandatory] Flag unnecessary line breaks inside comments, docstrings, or documentation paragraphs. Allow structured multi-line Go doc comments, examples, tables, and code blocks.
- Flag verbose comments or comments that restate the code. Use one concise line for a type or method description only when needed.
- Flag committed plan/planning files such as `plan_*.md`.

## Commits and pull requests

- Flag commit subjects that do not use the short Conventional Commit format `type(kebab-case-scope): Sentence case`.
- Flag long commit subjects.
- Flag pull request descriptions that do not use ASD-STE100 Simplified Technical English.
- Flag bug-fix descriptions that do not state the issue first and then give a short overview.
- Flag overview paragraphs longer than 2 or 3 lines.
- Flag change descriptions that do not use short, clear bullet points.
- Flag AI co-authors, AI session details, or AI agent metadata in commits and pull requests.

## Testing

- Flag tests that do not verify meaningful behavior, a risk, or a failure case.
- Flag tests added only to increase coverage numbers.
- Flag unclear test names.
- Flag tests that need comments but do not explain their intent clearly.
- Flag test comments that do not use ASD-STE100 Simplified Technical English or that restate the test code.
- Flag separate commits that add coverage without a related behavior or bug fix.
- Flag nondeterministic or interdependent tests.

## Go design and structure

- Flag Go changes that add structs, methods, interfaces, or package boundaries without a clear design in the change context. The design should identify ownership, state, method responsibilities, interfaces, and error flow before implementation.
- Flag scattered behavior: logic placed in unrelated helpers, duplicate same-prefix files, or new packages added when an existing owner package should contain the behavior.
- Flag interfaces introduced speculatively, interfaces with excessive methods, and interfaces defined away from their consumer without a strong reason.
- Flag Go packages without a useful package comment, and exported types, functions, methods, constants, or variables without useful doc comments.
- Flag exported Go doc comments that do not start with the declaration name.
- Flag more than one package comment in a Go package.
- Flag unclear Go package names, including names such as `util`, `common`, `misc`, and `interfaces`.
- Flag unnecessary `Get` prefixes on Go constructors or accessors.
- Flag Go errors that lose useful context or prevent callers from inspecting the original error.
- Flag blocking Go operations that do not accept `context.Context` as the first parameter when context control is needed.
- Flag global mutable state.
- Flag unclear ownership of goroutines, shutdown, or channel closing.
- Flag multiple owners for mutable state or temporary state that leaks across package boundaries.
- Flag methods that are broad, do too many unrelated things, or need verbose comments to be understood.
- Flag Go changes that do not use `gofmt`, or that add custom logic where the standard library or an existing package helper is sufficient.
- Flag relevant Go changes that do not run `go vet` or the race detector, especially concurrency changes.
- Preserve the independent module boundaries: `metal/` and `services/wg-mesh/cli/` each have their own `go.mod`.

## Other component structure

- Flag Python behavior placed outside the module, domain object, manager, or task that owns it.
- Flag logic in Python CLI commands or API routes that should be delegated to another layer.
- Flag Python public functions or important data structures without useful type hints.
- Flag broad or generic Python exceptions when a specific exception is appropriate.
- Flag mutable global state and circular imports.
- Flag dependency injection that adds complexity without reducing coupling.
- Flag clever code when clear code is practical.
- Flag implicit configuration when explicit configuration is practical.
- Flag Python code that ignores the domain model and adds unrelated helper objects.
- Flag Python functions much longer than about 25 lines when they can be split without harming readability.
- Flag Python files over 500 lines when they should be grouped or split.
- Flag crowded folders, repeated file prefixes, and lazy re-exports in package `__init__.py` files.
- Flag unnecessary abbreviations.
- Flag custom logic that duplicates a standard API or an existing repository helper.
- Flag new code when existing code can be deleted or simplified instead.
- Flag mutable state with more than one owner.
- Flag temporary state that leaks outside its object or module.
- Flag broad error handling that hides partial or corrupt state.
- Flag retries around operations that are not safe to repeat.
- Flag no-argument Python methods that return one noun-like value without using `@property`.
- Flag argument-taking or multi-step Python methods shaped as properties when a `get_<noun>()` method is clearer.
- Flag private Python methods that are private only because they have one caller.
- Flag boolean Python properties and methods without an `is_` or `has_` prefix.
- In `services/http-proxy/`, keep control-daemon behavior in `control/`, OpenResty and Lua behavior in `nginx/`, and tests in `tests/`.

## Reuse, size, and naming

- Prefer the smallest change that solves the problem. Flag duplication of standard APIs or existing repository helpers.
- Flag functions much longer than about 25 lines when they can be split without harming readability, and files over 500 lines when they should be grouped or split.
- Flag unnecessary abbreviations.

## Errors and tests

- Flag broad exception handling or fallback logic that hides corrupt or partial state.
- Flag retries around operations that are not safe to repeat.
- Require focused tests for changed behavior and preserve the original intent of existing tests.
