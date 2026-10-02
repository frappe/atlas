# Atlas Repository Guide

## Session continuity

- Treat this file as the primary instruction source for every task.
- After context compaction, summarization, restart, or loss of working context, read this file again before you continue.
- Preserve a reference to `CLAUDE.md` in a handover or context summary so the next context reloads it.
- Do not rely on remembered repository rules when this file is available.

Atlas is a monorepo for Frappe Cloud V2 VM infrastructure. It contains a Frappe app, Go services, an OpenResty proxy, and eBPF programs.

## Start here

Read the [root specification](SPEC.md) for the project layout and component map. Read the matching component `SPEC.md`, and the handbook page it links, before you make a structural change. The [code map](docs/develop/code-map.md) links each topic to its page, specification, code, and tests.

Re-read `CLAUDE.md` after context compaction or a resumed session before you continue work.

Read the [incident reports](docs/incidents/README.md) before you change a component. Do not repeat a past root cause. A person files an incident report. You can suggest a report, but do not add one unless a person asks. A filed report is immutable. Never edit it.

- [Atlas app](atlas/SPEC.md)
- [Metal](metal/SPEC.md)
- [HTTP proxy](services/http-proxy/SPEC.md)
- [IPv6 router](services/ipv6-router/SPEC.md)
- [WG Mesh](services/wg-mesh/SPEC.md)

## Core principles

- Keep changes small, direct, and in the component that owns the behavior.
- Keep decision-making visible to the user. Before implementation, explain the proposed design, assumptions, ownership, state, dependencies, error flow, risks, and important trade-offs. Use a small ASCII relationship diagram when it helps. Do not leave a material engineering decision unstated. Proceed after the user agrees with the plan.
- Do not change unrelated dirty files, generated artifacts, or local data.
- Do not add plan files, such as `plan_*.md`.
- Prefer clear, explicit code over clever code and unnecessary abstraction.
- Store mutable and temporary state in the object, task, or goroutine that owns its lifecycle. Do not duplicate mutable state across owners.
- Fail near the cause. Retry only operations that are safe to repeat.
- Do not add a dependency when the standard library or an existing repository dependency is sufficient.
- Do not commit secrets, private keys, tokens, `.env` content, or production credentials.
- Regenerate checked-in generated artifacts only when their source changes.
- Use `git mv` when you intentionally move or rename a tracked file.

## Temporary rules

Atlas is in active development and is not deployed to production.

- Do not preserve backward compatibility unless the task or specification requires it.
- Prefer the target design over compatibility layers, migration shims, deprecated aliases, or fallback behavior.
- Do not design for rolling upgrades, mixed-version deployments, or zero-downtime migration unless required.
- Revisit these rules before the first production deployment.

## Code style

### All code

- Use complete words for names. A Go method receiver can use a short name.
- Put blank lines between logical code blocks in a function.
- Put behavior with the type, module, or package that owns it.
- Extend an existing folder or package before you add a new one. Group related files and avoid crowded folders.
- Do not add generic `utils`, `helpers`, `common`, or similar folders.
- Do not add comments that repeat the code. Explain a business rule, invariant, external quirk, or concurrency rule only when needed.
- Add focused tests for meaningful behavior and failure cases. Do not add tests only to increase coverage.
- Keep tests deterministic and independent.

### Python

- Keep Python domain behavior in its domain object, manager, or task. Keep CLI commands and API routes thin.
- Use type hints for public functions and important data structures.
- Raise specific exceptions. Handle only errors that the code can recover from.
- Avoid mutable global state, circular imports, and dependency injection that does not reduce coupling.
- Prefer small functions and clear object-oriented code when it fits the domain.
- Use binary units. Name a field for its unit, such as `disk_mib` and `throughput_mibps`.
- Use `@property` only for a cheap, side-effect-free, no-argument operation that returns one noun-like value.
- Keep a method public when callers outside its owner use it. Prefix implementation-only methods with `_`. Use domain verbs instead of a generic `get_` name when they explain the operation.
- Name boolean properties and methods with `is_` or `has_`.

### Go

- For a Go change, use the approved plan, task, and specification as the design source. Ask the user when ownership, package boundaries, state, interfaces, concurrency, or error flow remain materially unclear.
- Prefer concrete types. Define a small interface at its consumer only when it has a real need.
- Add one useful package comment to each Go package. Add a doc comment to each exported declaration. Start an exported doc comment with its name.
- Use `NewType` for constructors. Do not use unnecessary `Get` prefixes.
- Wrap errors with useful context and preserve errors that callers inspect.
- Pass `context.Context` first to operations that can block.
- Avoid global mutable state. Define goroutine ownership, shutdown, and channel closing.
- Use standard-library and repository helpers before you add an abstraction.
- Run `gofmt`, focused tests, `go vet`, and the race detector when relevant. Keep Go module boundaries.

## Documentation

- Mandatory: Write each document for Atlas engineers and operators. Keep each paragraph short and focused on one idea.
- Mandatory: Split a long paragraph into short paragraphs whenever you edit its document.
- Write documentation for the reader who must use, change, or operate the system.
- Use ASD-STE100 Simplified Technical English. Use short, direct sentences and one term for one thing.
- Keep each Markdown prose paragraph on one source line. Do not use em dashes.
- Describe current behavior only. Do not describe removed behavior or old interfaces.
- Use a clear documentation tree: root README, component overview, focused topic documents, then detailed specifications when needed.
- Keep closely related behavior together. Do not split documentation only to make files smaller.
- Write behavior once, in the handbook under `docs/`. Use `SPEC.md` next to the code for the code contract: key files, types, extension points, invariants, and tests. Link to the handbook instead of repeating it. Keep a README to a few lines that link to both.
- Put detailed behavior in one authoritative location. Link from summaries to the detailed document and back when useful.
- Use headings that answer a reader question, such as Purpose, Configuration, Operation, or Validation.
- Use a list or table for more than 3 related items. Use a small ASCII diagram only when it makes a relationship easier to understand.
- Give an example when a command, API, configuration value, or workflow can otherwise be unclear.
- Explain an external term, flag, or mode on its first use when the reader might not know it.
- Before handover, check affected documentation for accuracy, current links, and enough context for the next reader to continue.
- Do not make documentation so short that it hides purpose, ownership, operation, or the next useful reference.
- Update the related documentation in the same pull request as a behavior, interface, operation, or layout change.

## Validation and handover

- Mandatory: Review every changed line before you make a commit. Remove complexity that the change introduces or exposes only when the work stays within task scope.
- For Go code, use the [Go code review guide](llm/go-code-review-guide.md). See [agent tooling setup](llm/README.md) for related skills.
- Validate untrusted input at its boundary. Use concrete types in trusted code.
- Remove unnecessary helpers, wrappers, forwarding layers, generic maps, interfaces, and defensive fallbacks when they do not represent a real need.
- Keep valid error handling, boundary validation, cleanup, synchronization, and security checks.
- Before handover, run the relevant formatter, tests, and static checks. Run the full component suite when feasible. Report targeted checks and their limits when it is not.
- In the handover, report the result, changed paths, and verification. Explain implementation details only when the user asks or the reason is not clear.

## Commits and pull requests

### Commits

- Use a short Conventional Commit subject: `type(scope): Sentence case`.
- Use one of `feat`, `fix`, `refactor`, `test`, `docs`, `build`, or `chore`. Use a component as the scope: `atlas`, `metal`, `http-proxy`, `ipv6-router`, `wg-mesh` or `wireguard-gateway`.
- Do not add an AI co-author, session data, or agent data.

### Commit sequence

Use this flow when a change needs more than one commit.

1. Plan the complete commit list first. Give each commit one purpose and a subject. Show the list to the user.
2. Make each commit build and pass its tests alone. Put a file in the first commit that needs it, and stage an earlier version of the file when a later commit completes it.
3. Review the diff of each commit before you stage it. Remove comments that repeat the code, and shorten verbose comments, docstrings, and documentation. Make names, structure, and error handling match the surrounding code of that component.
4. Stage one commit at a time. Show the staged paths and diff summary, then commit when the user says `next`.
5. Before each commit, check the staged tree, not the working tree. Export it with `git checkout-index -a --prefix=<directory>/` and run the formatter, static checks, and focused tests there. Report checks that run only on the working tree.
6. If a pre-commit hook changes files and stops the commit, stage the changed files and commit again.
7. After the last commit, confirm that the working tree has no unstaged change. Push only when the user asks.

### Pull requests

- For now, use the same Conventional Commit format for the pull request title.
- Keep the description short and use ASD-STE100 Simplified Technical English.
- Follow the validation and handover rules before you write the description.
- State what changed and why it matters. Do not narrate the implementation.
- Group related changes. Include visual evidence only for visual changes.

For a bug fix, use this structure:

```text
## Issue
<One sentence that states the user-visible or operational problem.>

## Summary
<1 or 2 sentences that state the fix and why it matters.>

## Why
<The technical or business reason for this approach.>

## What changed
- <Specific change>

## Screenshots
<Before and after evidence. Omit this section when it does not apply.>

## Related issues
Closes #<issue>
```

For a feature, use this structure:

```text
## Summary
<1 or 2 sentences that state the capability and why it matters.>

## Why
<The technical or business reason for this approach.>

## What changed
- <Specific change>

## Screenshots
<Before and after evidence. Omit this section when it does not apply.>

## Related issues
Refs #<issue>
```

## Agent tooling

Use the most specific available skill for the task. See [Frappe skills](llm/README.md#frappe-skills), [review skills](llm/README.md#review-skills), and [focused output](llm/README.md#focused-output) for sources and installation commands.

### Frappe skills

Use these skills when they are installed:

| Skill | Use for |
|---|---|
| `quality-code-review` | Frappe correctness, security, performance, concurrency, readability, API design, and test reviews. |
| `code-style` | All code edits and code-style questions. |
| `technical-writing` | Documentation, READMEs, commits, pull requests, and release notes in Simplified Technical English. |
| `ui-design` | General UI and UX work. |

Do not use `frappe-app-dev`.

### Review skills

Use `grill-me` for a strict review before you hand over a change.

Use `write-pr-description` to draft a structured pull request description.

Use `i-have-adhd` for focused, action-first output. Invoke it with your agent's skill command.

### Other skills

- `fastapi`: Use for FastAPI routes and Pydantic models.
- `ste100-writer`: Use for controlled technical English and operational documentation.
- `imagegen`: Use when a task needs a generated or edited bitmap image.
