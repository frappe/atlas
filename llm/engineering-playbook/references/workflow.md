# Workflow

## Modes

| Requester says | Mode | Do |
|---|---|---|
| "just asking", "just tell", "dont change/modify" | Answer | Answer briefly. Change nothing. |
| "propose/suggest/plan first", "lets discuss" | Proposal | Propose. Wait. |
| "review", "check", "audit", "prepare list" | Review | Ranked findings. Fix only on "fix those" or "do 2,3". |
| "go ahead", "do it", "less go", "implement", "fix" | Execute | Do it. Do not re-propose ("whatever i said do it man"). |
| "keep doing yourself", "going to sleep" | Autonomous | No questions. Keep a plan file with checkboxes. Still no commit. |
| "fix in place first", "patch it, i want to test" | Live test | Patch the development host to prove it, then code it, then revert the patch if asked. |
| "leave it", "later", "not now" | Drop | Stop. Do not raise it again this session. |

Ambiguous short request: one question, one line. Numbered answers to design questions: apply literally.

## Discover before you decide

When no solution direction is given (skip for small, obvious fixes):

1. **Investigate** with [thinking.md](thinking.md): code, docs, incidents, logs. Reproduce in development. Find how the codebase solves similar problems.
2. **Cross-question** in one message, one line each: goal, who is affected, scale, failure tolerance, compatibility, deadline, what was tried. Do not ask what code or defaults answer.
3. **Offer** two or three options with trade-offs; recommend one.
4. **Build** after the requester picks.

## Plan first

Before structural work, show design, assumptions, ownership, state, dependencies, error flow, risks and trade-offs. Add a small ASCII diagram when ownership or flow is unclear.

- **Go:** settle structs, interfaces and package boundaries first ("first give plan for metal side, then atlas").
- **Doctype or dialog:** ASCII mockup (sections, columns, field types) for any new doctype, field, move or section. See [code.md](code.md#doctype-and-dialog-layout).
- **Naming:** two or three options with a pick; accept the requester's choice.
- **Large work:** plan file in `.tmp/<topic>_plan.md` or root `final_plan.md`, `plan_fixes.md`, `report.md`. TODO checkboxes, ticked as done. Never committed.
- **Missing feature:** check if it exists; if not, plan first.
- **Better idea:** say it in a few lines; do not silently build it.
- Keep plans simple: no new daemon, hub, userspace service, address range or doctype when an existing block fits ("why we need the hub, its NxN na?").

## Scope

- Stay in the owning component. Ask before an `atlas/` or `metal/` task edits `services/`, `scripts/` or `clients/`.
- List unasked fixes as one-line follow-ups. Unrelated fixes get their own PR from `develop` (a worktree is fine).
- A fix owned by another repo (Pilot, Frappe, Cargo, Central): ask which repo owns it; separate PR.
- Leave untracked plan files and others' staged work alone.

## Git

- Never `git stash`, `reset --hard` or checkout over others' work. Stage only your hunks.
- `git mv` for moves. `--amend` plus `push --force-with-lease` on feature branches when agreed. `reset --soft` to re-review.
- Merging `develop`: resolve surgically; adapt to develop, never drop its changes.
- Re-landing someone's PR: keep their commits, authorship and dates.
- Use `gh`. PR bodies via heredoc or file (no literal `\n`). With `gh stack`, add one layer.

## Commit sequence

1. Plan the commit list (one purpose each, with subjects). Wait.
2. Stage one commit (partial staging is fine).
3. Show a per-file list: path, diff size, a few words.
4. Commit on `next`, "commit it" or "yes". On "auto commit", commit each and suggest the next.
5. Check the staged tree with formatter and static checks.
6. A hook rewrote files: stage them, commit again.
7. Confirm nothing is left unstaged. Push only when asked.

Messages:

- `type(scope): Sentence case`, short and meaningful. Types: `feat fix refactor perf test docs build chore`. Scope is a component only: `atlas`, `metal`, `wg-mesh`, `http-proxy`, `ipv6-router`, `wireguard-gateway`, `clients`.
- No lone dependency-bump commit, no word "clarify", no security detail in the subject.
- Body only when context matters: cause and final state, under about 300 to 400 characters, no logs or history.
- `Refs:` line at the end with links to official docs, man pages, RFCs, upstream issues or good engineering posts when they explain the change (for example the systemd file descriptor store page, RFC 5227). Never private links. Not counted in the body length. Reuse the links in the PR.
- No AI co-author or session line. Human co-author only on request.

## Pull requests

- Feature branch off `develop` (or the named base). Never push to `develop` unless told.
- Title: Conventional Commit. First line: `Closes #<issue>`, one per linked issue (omit if none). No related-issues section.
- Bug fix: `Issue`, `Summary`, `Why`, `What changed`, optional `Screenshots`. Feature: `Summary`, `Why`, `What changed`, optional `Screenshots`.
- Short and honest: a two or three line overview, then up to about 5 bullets by importance. Link docs for detail ("keep it smaller and compact man").
- "What changed" is for the reviewer: behavior users or operators notice, contract changes and whether old clients work, data or schema changes, risky areas (concurrency, security, lifecycle), rollout steps. Leave out file lists, renames, formatting, comment, test and doc edits, regenerated clients, lint fixes and non-behavioral refactor steps.
- No validation or ops sections. The solution is an overview; main fix first. Benchmarks get a short section when they matter. Keep removals out of the title.
- On request, watch CI and bots until clean ("keep checking and make it 5/5").

## Tests

- Test behavior that can regress. No constant-echo tests, no mock-heavy tests that catch nothing, no tests for guest scripts, ops scripts (`atlas-vm`), simulators or one-line SSH actions. Delete tests with their feature. Merge scattered test files.
- "No tests for now": skip, and do not mention it again.
- Run `pilot --site test.local run-tests --app atlas`. Never on `atlas.localhost` or Central's site. Use `pilot`, not `bench`.
- Go: `gofmt`, `go vet`, focused tests on touched packages, race detector when concurrency changed. Full suite on request.
- "Dont run tests": verify by reading.
- Fix the fixture, not production code, to make a test pass. Never weaken a test to match a bug; fix the behavior.

## Live servers and data

Tier rules: [environments.md](environments.md). Inside an allowed tier:

- Read first. Leave core service VMs (Cargo, proxy, object storage) alone unless the task is about them.
- Ask before provider API calls that create, delete or cost money (AWS, Scaleway), even in development.
- Before destructive work: backup or ZFS snapshot, runbook in an `upgrade-*.md` todo file, maintenance mode, graceful VM stop. Read incident reports first.
- Prefer patching over recreating. Never lose data.
- Clean up helper scripts, keys, test VMs and firewall rules; say what remains.
- Data fixes: `pilot migrate`, then patch through controllers (validation runs) or a migration patch. Back up first.
- After Python edits, restart every worker, not only `pilot start`.
- Never `pkill -f "<text>"` from a shell whose command line contains that text.
- Poll at the requested interval. Report in parts.

## Debugging

- Root cause first, then the sequence of events, then the preventive fix. Measure before optimizing.
- Wrong direction or "stop it, waste of time": stop and restart from evidence.
- Handoff notes: environment, hosts, commands, evidence, current state, in one compact block.
- Suggest an incident report for production-relevant failures; never write or edit one unless asked.

## Reporting

- One or two lines: result, changed paths, verification. Mention once what did not run (tests, deploy, migrate, worker restart).
- Staged commit: per-file list. "What did we do": group by objective, not by file.
- Explanations: simple words, small diagram on request.
- Status and handover stay terse ("i dont need to know the reason of change"). Proposals, reviews and "why" answers show the reasoning (evidence, invariants, options, failure paths) in chat. Never in code.
