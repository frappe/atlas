# Code review

## Before reviewing

1. Re-read `CLAUDE.md`, `.greptile/rules.md`, and for Go `llm/go-code-review-guide.md`.
2. Confirm scope (staged, unstaged, last commit, branch vs `develop`, PR); ignore the rest.
3. Read surrounding code and framework source when a claim depends on it. Read incident reports for the component.
4. Do not edit until told "fix those", "go ahead" or "do 2,3".

## What to report

Rank `P1` (blocks merge), `P2` (should fix), `P3` (minor), each with `file:line` and a one-line failure scenario. In order:

1. **Compatibility:** breaks an API or generated client, webhook consumer, eBPF map or wire format, Metal record, or existing data without a migration. Contract breaks are P1.
2. **Correctness:** wrong behavior, lost data, partial state on failure, unsafe retries, leaked sessions, goroutines, keys or cloud resources.
3. **Concurrency:** stale saves, two writers, unlocked select-then-update, network under a lock, inconsistent lock order, long transactions, non-idempotent retries, cross-site lock names ([design.md](design.md#concurrency-and-performance)).
4. **Security:** missing permission checks, tenant leaks, untrusted input to ORM or shell, secrets in URLs or logs, `0.0.0.0`, `Host` trust.
5. **Consistency:** a second pattern for an existing job, or a better pattern applied in only one place.
6. **Design drift:** two owners, logic in the API layer, provider code outside the provider, duplicate concepts, unasked knobs.
7. **Simplicity:** inflated lines, single-use helpers, wrappers, dead branches, constant tables, file sprawl. Show the shorter shape.
8. **Naming:** abbreviations, misleading names, two words for one concept, inconsistent verbs or statuses, stale test file names.
9. **Comments and docs:** restating, change-explaining or historic comments, file-top comments, stale or duplicated docs, wrapped Markdown, em dashes.
10. **Tests:** failing, flaky, stale fixtures, constant-echo, mock-heavy. Do not demand excluded tests.

"Dont nitpick" means design and structure only. "Think like a senior engineer" means abstractions, seams and ownership.

## Settled items

Do not re-raise the [accepted risks and settled items](domain.md#accepted-risks) as they stand. Flag a change that touches one, worsens it or breaks its assumption. Record a newly accepted risk there with its assumption and revisit trigger.

## Bot and agent findings

Greptile is "not always correct".

1. Judge each finding against the code and settled items. Ask "will it happen ever" in real use.
2. Valid and in scope: smallest code fix. Prefer handling it in code over arguing.
3. Invalid or by design: no code change. Short thread reply when asked (`@greptile` via `gh`).
4. Low value but valid: a one-line comment is fine when the team says so.
5. Mid-flow commit findings: keep a required commit (draft before remote call) with a one-line reason.
6. Missing docstrings on public classes: add one-line docstrings.
7. Repeat until clean or 5/5 when asked. Report new findings caused by a fix; do not loop silently.

When the team rebuts your finding correctly, withdraw it plainly.

## Contributor reviews

1. Read commit by commit; note what inflated the line count.
2. Find design errors: two authorities, reimplemented kernel or framework behavior, leaky statuses or flags, cgo, checked-in generated objects, single-implementation layers.
3. Propose the simpler shape: delete, merge, rename, keep.
4. Refactor after approval, keeping contracts, eBPF formats and data compatible.
5. Re-landing: keep their commits and authorship; add yours on top.

## Output

Ranked findings with `file:line`, scenario and a one or two line fix. Then "no issue found in: ..." if useful. Then what ran and its limits. No praise, no diff restatement.
