# Atlas Repository Guide

Atlas is a monorepo for Frappe Cloud V2 VM infrastructure. It contains a Frappe/Python app, Go services, an OpenResty-based HTTP proxy, and eBPF C programs. Keep changes small, direct, and local to the component being changed.

## Communication and change style

- Be concise. Report the result and relevant verification; do not narrate every step.
- Explain why a change was made only when the reason is not clear from the code or the request.
- Read `SPEC.md` before making structural changes.
- Do not touch unrelated dirty files, generated artifacts, or local data.
- Do not add plan or planning markdown files such as `plan_*.md`.
- Use `apply_patch` for manual edits.
- Add blank lines between logical sections of code to keep methods readable.

## Comments and documentation

- Write comments, docstrings, and documentation in ASD-STE100 Simplified Technical English.
- Use short, direct sentences and one term for one thing. Keep technical names and exact values unchanged.
- Do not use em dashes in comments, docstrings, or documentation.
- Avoid unnecessary line breaks inside comments, docstrings, and documentation paragraphs.
- Allow structured multi-line Go doc comments, examples, tables, and code blocks.
- Do not add explanatory comments at the top of a file. Exceptions are required Go package comments, build directives, and license headers.
- Keep comments and documentation concise. Explain only information that the code does not make clear.
- Update the relevant documentation when behavior, interfaces, operations, or structure changes.
- Keep each documentation file focused on one topic. Split a file when it becomes too large or covers several topics.
- Keep each `SPEC.md` file short. Use it as a router to focused component documentation.

## Commits and pull requests

- Use a short Conventional Commit subject in this format: `type(scope): Sentence case`.
- Use a kebab-case scope, such as `feat(metal-vm): Add snapshot support`.
- Keep the commit subject short. Use `feat`, `fix`, `refactor`, `test`, `docs`, `build`, or `chore` when appropriate.
- Keep pull request descriptions short and use ASD-STE100 Simplified Technical English.
- For a bug fix, first state the issue. Then give a short overview of the change.
- Keep the overview to one short paragraph of no more than 2 or 3 lines.
- List changes with short, clear bullet points.
- Do not add an AI agent as a co-author. Do not add AI session or agent metadata.

## Testing

- Write tests for meaningful behavior, risks, and failure cases.
- Do not add tests only to increase coverage numbers.
- Keep test names clear and easy to understand.
- Add a short comment when the test intent is not clear from its name and setup.
- Keep test comments in ASD-STE100 Simplified Technical English. Do not restate the test code.
- Keep test changes with the related code change when practical. Do not create a separate coverage-only commit.
- Keep tests deterministic and independent.

## Monorepo boundaries

- Root `atlas/` is the Frappe app and Python code. Keep its domain behavior in the existing domain objects, managers, and tasks; keep CLI commands and API routes thin.
- `metal/` is an independent Go module for the host-side `metald` daemon and VM operations. Its implementation is organized under `metal/internal/`; the executable is under `metal/cmd/metald/`.
- `services/wg-mesh/cli/` is an independent Go module for the WireGuard/eBPF mesh CLI. The eBPF C source and headers are under `services/wg-mesh/bpf/`.
- `services/http-proxy/` is a standalone proxy component. Its control daemon is Python, while the data plane is OpenResty/Nginx and Lua.
- Each component owns its tests and documentation. Do not move code across these boundaries without a clear interface and updated docs.

## Go rules

- Before writing Go code, plan the design with the user: identify the structs, their ownership and state, methods, interfaces, package boundaries, and error flow. Proceed with implementation only after that design is settled.
- Avoid scattering. Put behavior beside the type or package that owns it; extend an existing package before adding a new package or same-prefix file.
- Keep interfaces small and define them where they are consumed. Do not introduce an interface speculatively.
- Add a useful package comment to every Go package. Add useful doc comments to every exported type, function, method, constant, and variable.
- Start an exported Go doc comment with the name of the declaration.
- Keep one package comment in one file for each Go package.
- Use clear, short package names. Avoid names such as `util`, `common`, `misc`, and `interfaces`.
- Use `NewType` for Go constructors. Do not add unnecessary `Get` prefixes.
- Wrap Go errors with useful context. Preserve errors that callers must inspect.
- Pass `context.Context` as the first parameter to Go operations that can block.
- Avoid global mutable state.
- Define ownership for goroutines, shutdown, and channel closing.
- Prefer standard library and existing package helpers over custom abstractions.
- Keep methods focused and explicit. Use one concise line to describe a method when a comment is needed. Do not write verbose comments or comments that restate the method body.
- Run `gofmt` on changed Go files and add or update focused tests for behavior changes.
- Run `go vet` and the race detector for relevant Go changes, especially concurrency changes.
- Preserve module boundaries: run Go commands from `metal/` or `services/wg-mesh/cli/` as appropriate. There is no root Go module.

## Other component rules

- Put Python behavior in the module, domain object, manager, or task that owns it.
- Keep Python CLI commands and API routes thin.
- Use the repository formatter and linter as the source of truth.
- Use type hints for public Python functions and important data structures.
- Raise specific exceptions and handle only errors that the code can recover from.
- Avoid mutable global state and circular imports.
- Use dependency injection only when it reduces coupling.
- Choose clear code over clever code.
- Prefer explicit configuration over implicit behavior.
- Prefer object-oriented code when it matches the domain.
- Keep functions small. Around 25 lines is a useful target, not a strict limit.
- Keep files below 500 lines when practical.
- Group related files in subfolders. Avoid crowded folders and repeated file prefixes.
- Avoid lazy re-exports in package `__init__.py` files.
- Avoid abbreviations.
- Reuse standard APIs and existing repository helpers.
- Delete or simplify existing code before adding new code.
- Keep one owner for mutable state.
- Keep temporary state inside its object or module.
- Fail near the cause of an error. Do not hide partial or corrupt state.
- Retry only operations that are safe to repeat.
- For a no-argument method that returns one noun-like value, prefer `@property`.
- For methods with arguments or multiple steps, use `get_<what_it_returns>()` when it fits.
- Keep methods public by default. Use a leading underscore only for truly internal behavior.
- Do not make a method private only because it has one caller.
- Name boolean properties and methods with `is_` or `has_`.
- Add or update tests for behavior changes.
- Keep HTTP proxy control code in `services/http-proxy/control/`.
- Keep OpenResty and Lua code in `services/http-proxy/nginx/`.
- Run targeted tests for Python changes.

## Validation

- Root app: use the Frappe bench commands described by the CI workflow in `.github/workflows/atlas-ci.yml`.
- HTTP proxy: from `services/http-proxy/`, run `python -m pytest -q tests` with the Docker test stack when required.
- WG Mesh: from `services/wg-mesh/`, use `make bpf` or `make build`; run Go tests from `services/wg-mesh/cli/`.
- Metal: from `metal/`, run `go test ./...` and the relevant integration tests when host dependencies are available.
- Update the component documentation when behavior, interfaces, operations, or folder ownership changes.
