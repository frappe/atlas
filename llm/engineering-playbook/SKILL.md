---
name: engineering-playbook
description: Atlas team workflow and engineering judgment for code changes, refactors, reviews, design, docs, commits, PRs, and host or site commands. Covers Atlas, Metal, related services, Pilot, Central, and Cargo.
---

# Engineering playbook

`CLAUDE.md` is the primary rulebook. This skill holds the team's working method and decisions drawn from developer sessions. Follow `CLAUDE.md` on conflict and state the conflict briefly.

Read only the references relevant to the task, fully on first use. Before a host, site, or cloud command, read [environments.md](references/environments.md). Before a non-trivial change or refactor, use [thinking.md](references/thinking.md). Before handover, use [review.md](references/review.md).

| Task | Reference |
|---|---|
| Problem solving and refactoring | [thinking.md](references/thinking.md) |
| Scope, planning, git, tests, commits, PRs, handover | [workflow.md](references/workflow.md) |
| Development, staging, production access | [environments.md](references/environments.md) |
| Code and refactoring conventions | [code.md](references/code.md) |
| Architecture, state, APIs, concurrency, security | [design.md](references/design.md) |
| Failure, recovery, operations, user experience | [reliability.md](references/reliability.md) |
| Diff and review-bot findings | [review.md](references/review.md) |
| Documentation and written text | [docs.md](references/docs.md) |
| Rejected patterns and developer quotes | [anti-patterns.md](references/anti-patterns.md) |
| Atlas facts and accepted risks | [domain.md](references/domain.md) |

## Rules

1. **Terse status, complete reasoning.** Lead with the result. Keep routine replies short; give enough context for design, review, and teaching. Reasoning goes in chat, at most a compact commit body, never in code.
2. **Minimal, clear diff.** Prefer fewer lines without dense code. Avoid single-use wrappers, generic helpers, magic constant tables, extra layers, and unused knobs.
3. **Useful comments only.** Explain invariants, external quirks, and non-obvious behavior; never restate code, describe the change, or add file-top comments.
4. **Keep contracts compatible.** Old clients, hosts, and data must work. Add optional API fields; do not rename, remove, or retype existing fields, routes, statuses, or error codes, change relied-on defaults, or add an API version. Avoid eBPF map and wire breaks. Explain an unavoidable break and its upgrade path first; add a migration patch for schema or data changes.
5. **Discover before structural work.** Investigate and offer options when no solution is given. Show the design and wait for agreement. "Just tell", "propose first" and "dont modify" mean no edits.
6. **Never commit or push unprompted.** Stage one planned commit at a time with a per-file summary; raise a PR only when asked. Do not add an AI co-author, session trailer, or generated-with line.
7. **Name precisely.** Use full words and one term per concept across code, docs, API, and UI.
8. **Fail near the cause.** Do not hide errors with broad catches or silent side effects. Log and continue per item in retrying background loops.
9. **Document current behavior once.** One source line per Markdown paragraph; no em dash or duplicate explanation.
10. **Verify claims.** Check the code, framework source, or allowed development host before asserting behavior.
11. **Stay in scope.** Do not alter unrelated files, another person's staged work, or another repo; never stash their work.
12. **One pattern per job.** Follow the existing pattern. Replace all uses when a better pattern fits within scope; otherwise propose a separate refactor.
13. **Design for concurrency.** Prevent stale saves, version conflicts, lock timeouts, deadlocks, and duplicate allocations.
14. **Approve doctype layout first.** Show a mockup. Keep forms balanced and grouped, with short descriptions shown on click.
15. **Respect environment tiers.** Development actions are delegated; ask before each staging write; never act on production or undeclared resources. Run Frappe tests only on `test.local`; run focused Go tests on touched packages and the full suite on request.

## Pre-handover checklist

- [ ] The diff solves the request with no unrelated changes or unnecessary complexity.
- [ ] Existing clients, hosts, and data remain compatible; eBPF formats remain compatible or have an agreed upgrade path; schema and data changes have a migration patch.
- [ ] Concurrency is safe: one writer per field; no stale save after slow work; select-then-update is locked; locks have a fixed order and short lifetime; no remote call holds a database lock; retries are safe. Run `go test -race` for Go concurrency changes.
- [ ] The [reliability pre-mortem](references/reliability.md#pre-mortem) covers repeat, partial failure, capacity, observability, recovery, rollout, and user-visible states.
- [ ] The existing pattern is followed, or all affected uses are updated. Any doctype or dialog layout change was approved.
- [ ] Names are clear; logical code blocks have space; comments and docstrings carry only useful context.
- [ ] Affected docs describe current behavior accurately, with no duplicate explanation or broken links.
- [ ] Relevant formatter, static checks, and focused tests ran, or the handover states their limits. Regenerate clients and other checked-in output when their source changes.
- [ ] Every command used an allowed environment. Restart all relevant Python workers when checking changed behavior live.
- [ ] Report the result, changed paths, verification, and material limitations. Show staged paths before a requested commit.
