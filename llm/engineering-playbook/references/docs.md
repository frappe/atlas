# Docs and written text

Covers handbook pages, SPEC files, READMEs, field descriptions, commit and PR text. Use `technical-writing` or `ste100-writer` for wording.

## Style

- ASD-STE100: short sentences, active voice, present tense, one term per thing, no idioms.
- One paragraph per source line; never hard-wrap.
- No em or en dashes; avoid semicolon chains.
- Small paragraphs; split long ones in any file you touch. Lists or tables for more than three items.
- Plain words; explain external terms on first use.
- Sentence-case headings that answer a reader question.

## What to write

- Current behavior only. No history, "previously" or trace of old code.
- Skip internal or expected behavior ("natural expectation").
- One sentence per fact; fold clarifications into existing sentences; no rationale paragraphs.
- Do not over-trim: keep the mechanism engineers need ("i told you to trim, doesnt mean you remove everything"). Restore a better old version when asked.
- Explain why a design exists when readers would get it wrong (controllerless WG Mesh, the IPv6 router, gateway vs SNAT).
- User guides say what the reader does, not internals.
- No limitations section unless asked.
- No PII in public text: names, emails, tenant hostnames, IDs or IPs.

## Where docs live

- **Handbook** (`docs/`, VitePress): behavior and how-to, written once.
- **SPEC.md** beside code: key files, types, extension points, invariants, tests; links to the handbook. A router, not an explanation.
- **README:** a few lines linking both. Doctype READMEs in the doctype folder.
- **API docs** from OpenAPI; the Markdown page is a short overview linking the reference.
- No duplication ("i hate duplication anywhere"). Split multi-topic docs; merge thin ones.
- Incident reports: one per incident in `docs/incidents/`, filed by a person, full hash of the last affected commit, no Actions or Resolution section, immutable.

## Handbook passes

- Natural, shallow sidebar with labels of similar length.
- What and why first, then building blocks bottom up (underlay, WireGuard, WG Mesh, privileged VM, gateway VM, services).
- Concept, then guide, then reference. Even small features get an overview.
- Small diagrams, Mermaid when ASCII looks bad, readable without horizontal scroll, real address formats.
- Sample code for formats readers must decode (auto-proxy labels, IPv6 router mapping).
- Link good external references (kernel, systemd).
- Clean minimal home page.
- Verify every claim against the code.

## Comments

See [code.md](code.md#comments-and-docstrings).

## Desk field descriptions

One short line (`<br>` only if truly needed), shown on click. No warnings under fields. Labels match the type.

## Commit and PR text

See [workflow.md](workflow.md#commit-sequence) and [pull requests](workflow.md#pull-requests).

## Plans and reports

Plan files (`.tmp/*_plan.md`, `final_plan.md`, `plan_fixes.md`, `report.md`, `upgrade-*.md`) are temporary, use checkboxes, skip doc rules and are never committed. Activity reports: bullets, verified PR links, grouped by project.
