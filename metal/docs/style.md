# Metal documentation style

How to write SPEC and docs files under `metal/`. The root `atlas/CLAUDE.md` governs
code. This file governs the docs.

## Two layers

- Package `SPEC.md` is the detailed layer: types, ownership, call flow, diagrams.
- `docs/*.md` is the broad layer: an overview with enough detail to orient a reader.
- Detail lives in exactly one place, the SPEC. The docs summarize and route to it.
- Cross-link both ways. Each `docs/*.md` section links to the owning SPEC for the
  full detail. Each SPEC links back up to its concept doc for the broad picture.

## Diagrams

- ASCII only. Boxes and arrows for flows, state machines, and dependency graphs.
  ASCII trees for filesystem and network-topology layouts.
- Give every broad structure, concept, or flow a diagram.

## Tone and words

- Plain. Not curt, not friendly. Bare minimum prose. What fits in 10 words does
  not take 100.
- ASD-STE100 Simplified Technical English. Short sentences. One term for one thing.
- No em dashes. Use a colon, or split into two sentences.
- Spell out "virtual machine" on first mention in a file's Purpose. "VM" is fine
  after that, and in tables, diagrams, and headings.
- Gloss a non-obvious external term, flag, or mode in one line on first use, for
  example a systemd job mode or a ZFS property. Do not assume the reader knows it.

## SPEC skeleton

```text
# <package>: <one-line purpose>
[<parent> SPEC](../SPEC.md) · overview: [docs/<concept>.md](../../docs/<concept>.md)

## Purpose        1-2 sentences
## Types          key types and which type owns which state
## <Diagram(s)>   ASCII: state machine / flow / topology / dataset lineage
## Related        links to the concept doc and sibling SPECs
```

- No "Dependencies" section. Put dependency direction in the `internal/` graph and
  in the Related links.
- Lead the Purpose with the premise: the one core idea the package rests on, before
  the mechanics. State it in a sentence or two.
- Put design rationale in the concept doc, not the SPEC. A "Design notes" section in
  the `docs/*.md` overview holds the "why", one or two tight lines each. The SPEC
  states the premise in its Purpose and otherwise stays mechanics.
- Parent and root SPEC files are routers. Keep them short. Link to children and
  concept docs.

## Keep in sync

Update the relevant SPEC and docs in the same PR as any change to behavior,
interface, operation, or layout. metal is under active development.

## Commits

- Subject: `docs(<scope>): Sentence case`.
- No AI co-author and no session metadata (root `CLAUDE.md` rule).
