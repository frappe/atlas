---
name: simplified-technical-english
description: Writing rules for every reply, report, and saved investigation, based on ASD-STE100 Simplified Technical English. Use for all text that an Atlas engineer reads.
---

# Write in Simplified Technical English

These rules are a short form of [ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/), Issue 9 (2025), from ASD (the AeroSpace, Security and Defence Industries Association of Europe). They do not replace the standard. The standard and its dictionary are free to request at <https://www.asd-ste100.org/>.

## Reply shape

- Write a bullet list. One fact or one action in each bullet.
- Put the most important bullet first.
- No preamble, no restated question, and no closing summary.
- Use a numbered list for steps that must be done in order.
- Use a table for 2 or more items with the same fields.
- Show a cause chain as an ASCII diagram in a `text` code block.

## Words

- Use one term for one thing. Do not use synonyms. Example: always "host", never "server" and "node" for the same machine.
- Use a word with one meaning only.
- Use the short common word: "start", not "initiate"; "use", not "utilize"; "show", not "indicate"; "before", not "prior to"; "about", not "approximately"; "need", not "require".
- Do not use an "-ing" word as a noun or an adjective. Write "before you restart the host", not "before restarting the host". A technical name is an exception.
- Use at most 3 nouns in a row. Use prepositions to break a longer cluster.
- Keep the articles "a", "an", and "the".
- Give the full term of an abbreviation at its first use, unless an Atlas engineer knows it, such as VM, CPU, or ZFS.
- Do not use a slash. Write "and" or "or".

## Sentences

- An instruction has at most 20 words. A description has at most 25 words.
- One instruction in one sentence. Start an instruction with the verb: "Restart metald on waw-1."
- Use the active voice. Use the passive voice only when the agent is not known.
- Use the simple present, simple past, or simple future tense. Write "the VM stopped", not "the VM has been stopped".
- Put a condition before its action: "If the pool is degraded, replace the disk."
- No semicolons, no em dashes, and no parentheses that add a second idea.

## Warnings

- Put a warning before the step that it applies to.
- Start it with the risk: "CAUTION: This stops all VMs on the host. Drain the host first."

## Accuracy

- Do not change a name, an ID, an IP address, a number, a unit, or a command to obey a rule.
- If a rule and the technical accuracy conflict, keep the accuracy.
